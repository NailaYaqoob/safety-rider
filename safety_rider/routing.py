"""Cooler-route recommendation: compare candidate routes by heat exposure.

The rider question this answers is not "what is the fastest way there" — their
navigation app already does that — but "which of these ways will cook me least".

Three things make it harder than scoring a line on a map:

**OSRM's public demo returns no alternatives.** ``alternatives=3`` yields a
single route even on a 28 km trip; the demo server filters them out. So
candidates are generated here instead, by routing *via* waypoints offset
perpendicular to the direct line. A detour to one side picks up a different set
of streets, which is exactly the variation we want to compare.

**Heat data is billed per area, not per point.** Sampling 200 route vertices
would be 200 lookups. Samples are therefore deduplicated by heat-grid cell
before anything is fetched, so a route crossing two cells costs two lookups no
matter how long it is — and :data:`settings.max_route_cells` caps even that, so
a cross-country request cannot quietly spend a fortune.

**Exposure is cumulative, not peak.** A route that touches 41 °C for thirty
seconds is safer than one that sits at 37 °C for twenty minutes. Routes are
scored in *degree-hours above the high-heat line* — the area under the curve,
weighted by how long the rider actually spends in each cell.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import settings
from .heat_layer import grid_key, is_in_coverage
from .models import RiderLocation
from .temperature_service import TemperatureReading, get_hyperlocal_temperature

log = logging.getLogger(__name__)

_M_PER_DEG_LAT = 111_320.0

#: A detour is only worth offering if it is not absurd. A route more than this
#: multiple of the fastest option's duration is dropped however cool it is —
#: nobody rides 40 minutes to avoid 2 °C.
MAX_DETOUR_RATIO = 1.6

#: Below this, two routes are the same road and offering both is noise.
MIN_DEGREE_HOURS_GAIN = 0.15


@dataclass
class RouteCandidate:
    """One candidate path, with its heat cost once scored."""

    label: str
    distance_m: float
    duration_s: float
    #: ``[[lon, lat], ...]`` — GeoJSON order, straight from OSRM.
    coordinates: list[list[float]] = field(default_factory=list)

    # Filled in by :func:`score_candidate`.
    degree_hours: float | None = None      # °C·h above the high-heat line
    peak_c: float | None = None
    mean_c: float | None = None
    cells_sampled: int = 0
    source: str = "unknown"

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0

    @property
    def duration_min(self) -> float:
        return self.duration_s / 60.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "distance_km": round(self.distance_km, 2),
            "duration_min": round(self.duration_min, 1),
            "degree_hours": None if self.degree_hours is None else round(self.degree_hours, 2),
            "peak_c": self.peak_c,
            "mean_c": self.mean_c,
            "cells_sampled": self.cells_sampled,
            "source": self.source,
            "coordinates": self.coordinates,
        }


@dataclass
class RouteComparison:
    """The result of comparing candidates: what to ride, and why."""

    fastest: RouteCandidate
    coolest: RouteCandidate
    candidates: list[RouteCandidate]
    #: True when the coolest route is meaningfully cooler than the fastest.
    worth_detour: bool
    reason: str

    def to_whatsapp_text(self) -> str:
        """A rider-facing summary. Short — they are reading this on a bike."""
        if not self.worth_detour:
            return (
                f"I checked {len(self.candidates)} route(s). The direct one "
                f"({self.fastest.distance_km:.1f} km, {self.fastest.duration_min:.0f} min) "
                f"is already the coolest — no better detour to offer."
            )

        extra_min = self.coolest.duration_min - self.fastest.duration_min
        saved = (self.fastest.degree_hours or 0) - (self.coolest.degree_hours or 0)
        return (
            f"🧭 *Cooler route found*\n\n"
            f"Direct: {self.fastest.distance_km:.1f} km, "
            f"{self.fastest.duration_min:.0f} min, peak {self.fastest.peak_c:.1f} °C\n"
            f"Cooler: {self.coolest.distance_km:.1f} km, "
            f"{self.coolest.duration_min:.0f} min, peak {self.coolest.peak_c:.1f} °C\n\n"
            f"About {extra_min:.0f} min longer, but roughly *{saved:.1f} fewer "
            f"°C-hours* of high heat."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "worth_detour": self.worth_detour,
            "reason": self.reason,
            "fastest": self.fastest.to_dict(),
            "coolest": self.coolest.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
        }


# ───────────────────────────────────────────────────────── candidate routes


def _perpendicular_offsets(
    start: RiderLocation, end: RiderLocation, offsets_m: tuple[float, ...]
) -> list[tuple[float, float]]:
    """Waypoints placed either side of the direct line's midpoint.

    Routing through one of these forces OSRM onto a different set of streets,
    which is how we manufacture the alternatives its demo server will not give
    us. Returns ``(lat, lon)`` pairs.
    """
    m_per_deg_lon = _M_PER_DEG_LAT * max(
        math.cos(math.radians((start.latitude + end.latitude) / 2)), 0.01
    )

    dx = (end.longitude - start.longitude) * m_per_deg_lon
    dy = (end.latitude - start.latitude) * _M_PER_DEG_LAT
    length = math.hypot(dx, dy)
    if length < 1.0:
        return []

    # Unit vector perpendicular to the direct line.
    px, py = -dy / length, dx / length
    mid_lat = (start.latitude + end.latitude) / 2
    mid_lon = (start.longitude + end.longitude) / 2

    points: list[tuple[float, float]] = []
    for offset in offsets_m:
        for sign in (1, -1):
            points.append((
                mid_lat + (py * offset * sign) / _M_PER_DEG_LAT,
                mid_lon + (px * offset * sign) / m_per_deg_lon,
            ))
    return points


def fetch_candidates(
    start: RiderLocation,
    end: RiderLocation,
    *,
    max_candidates: int = 4,
) -> list[RouteCandidate]:
    """Ask OSRM for the direct route plus detours through offset waypoints.

    Network failures are swallowed per-request: one unreachable detour must not
    cost the rider the whole answer. Returns at least the direct route, or an
    empty list if even that fails.
    """
    base = settings.osrm_base_url.rstrip("/")
    profile = settings.osrm_profile
    candidates: list[RouteCandidate] = []

    def _request(points: list[tuple[float, float]], label: str) -> RouteCandidate | None:
        coords = ";".join(f"{lon},{lat}" for lat, lon in points)
        url = f"{base}/route/v1/{profile}/{coords}"
        try:
            with httpx.Client(timeout=settings.osrm_timeout_s) as http:
                response = http.get(url, params={"overview": "full", "geometries": "geojson"})
            if response.status_code != 200:
                log.warning("OSRM %s for %s", response.status_code, label)
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("OSRM request failed (%s): %s", label, exc)
            return None

        routes = data.get("routes") or []
        if data.get("code") != "Ok" or not routes:
            log.warning("OSRM returned %s for %s", data.get("code"), label)
            return None

        route = routes[0]
        return RouteCandidate(
            label=label,
            distance_m=float(route.get("distance", 0.0)),
            duration_s=float(route.get("duration", 0.0)),
            coordinates=(route.get("geometry") or {}).get("coordinates", []),
        )

    direct = _request([(start.latitude, start.longitude), (end.latitude, end.longitude)], "Direct")
    if direct:
        candidates.append(direct)

    # Offsets scale with trip length: a 1 km hop needs a 200 m nudge, a 20 km
    # ride needs kilometres before it meets different streets.
    span_m = _haversine_m(start, end)
    offsets = tuple(sorted({max(200.0, span_m * f) for f in (0.15, 0.35)}))

    for index, (lat, lon) in enumerate(_perpendicular_offsets(start, end, offsets)):
        if len(candidates) >= max_candidates:
            break
        detour = _request(
            [(start.latitude, start.longitude), (lat, lon), (end.latitude, end.longitude)],
            f"Detour {index + 1}",
        )
        if detour and not _is_duplicate(detour, candidates):
            candidates.append(detour)

    return candidates


def _is_duplicate(candidate: RouteCandidate, existing: list[RouteCandidate]) -> bool:
    """Within 2% on both distance and duration is the same road."""
    for other in existing:
        if other.distance_m <= 0:
            continue
        if (
            abs(candidate.distance_m - other.distance_m) / other.distance_m < 0.02
            and abs(candidate.duration_s - other.duration_s) / max(other.duration_s, 1) < 0.02
        ):
            return True
    return False


def _haversine_m(a: RiderLocation, b: RiderLocation) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(a.latitude), math.radians(b.latitude)
    dp = p2 - p1
    dl = math.radians(b.longitude - a.longitude)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ──────────────────────────────────────────────────────────────── scoring


def sample_cells(candidate: RouteCandidate, max_cells: int) -> list[tuple[float, float]]:
    """Distinct heat-grid cells the route passes through, in order.

    This is the cost control. A 200-vertex route collapses to however many
    grid cells it actually crosses — usually one or two — and each cell is a
    cached lookup rather than a billed request.
    """
    seen: list[tuple[float, float]] = []
    for lon, lat in candidate.coordinates:
        cell = grid_key(lat, lon)
        if cell not in seen:
            seen.append(cell)

    if len(seen) <= max_cells:
        return seen

    # Spread the cap ACROSS the route rather than truncating at the front.
    # Truncating scored only each candidate's opening stretch — and every
    # candidate leaves from the same place, so distinct routes came back with
    # identical scores and the comparison was meaningless. Always keep the
    # first and last cell so origin and destination are represented.
    step = (len(seen) - 1) / (max_cells - 1) if max_cells > 1 else 0
    picked = [seen[min(len(seen) - 1, round(i * step))] for i in range(max_cells)]
    deduped: list[tuple[float, float]] = []
    for cell in picked:
        if cell not in deduped:
            deduped.append(cell)
    return deduped


def score_candidate(candidate: RouteCandidate, max_cells: int | None = None) -> RouteCandidate:
    """Attach heat cost to a candidate, in °C·hours above the high-heat line.

    Each cell the route crosses contributes ``(temp - threshold) * time_in_cell``
    when it is over the line, and nothing when it is not. Time in a cell is
    approximated as an equal share of the trip — routes here are short enough
    that speed is roughly constant, and the alternative is a per-segment
    traversal that costs more code than the extra precision is worth.
    """
    from .heat_risk import OSHA_HIGH_C

    limit = max_cells if max_cells is not None else settings.max_route_cells
    cells = sample_cells(candidate, limit)
    if not cells:
        return candidate

    readings: list[TemperatureReading] = []
    for lat, lon in cells:
        if not is_in_coverage(lat, lon):
            continue
        reading = get_hyperlocal_temperature(lat, lon)
        if reading.ok:
            readings.append(reading)

    if not readings:
        return candidate

    hours_total = candidate.duration_s / 3600.0
    share = hours_total / len(readings)

    degree_hours = sum(max(0.0, r.celsius - OSHA_HIGH_C) * share for r in readings)
    candidate.degree_hours = degree_hours
    candidate.peak_c = round(max(r.celsius for r in readings), 1)
    candidate.mean_c = round(sum(r.celsius for r in readings) / len(readings), 1)
    candidate.cells_sampled = len(readings)
    candidate.source = readings[0].source
    return candidate


def compare_routes(start: RiderLocation, end: RiderLocation) -> RouteComparison | None:
    """Fetch, score, and rank candidate routes. None if routing is unavailable.

    Blocking — both OSRM and the heat lookups are synchronous. Call it from a
    worker thread, never off the event loop.
    """
    candidates = fetch_candidates(start, end)
    if not candidates:
        log.warning("No routes available between the two points.")
        return None

    for candidate in candidates:
        score_candidate(candidate)

    scored = [c for c in candidates if c.degree_hours is not None]
    if not scored:
        # Routing worked but heat data did not. Still tell the rider the way.
        fastest = min(candidates, key=lambda c: c.duration_s)
        return RouteComparison(
            fastest=fastest, coolest=fastest, candidates=candidates,
            worth_detour=False, reason="no heat data available along these routes",
        )

    fastest = min(scored, key=lambda c: c.duration_s)

    # Only consider detours a rider would actually accept.
    acceptable = [
        c for c in scored
        if c.duration_s <= fastest.duration_s * MAX_DETOUR_RATIO
    ]
    coolest = min(acceptable or [fastest], key=lambda c: c.degree_hours or 0.0)

    gain = (fastest.degree_hours or 0.0) - (coolest.degree_hours or 0.0)
    if coolest is fastest or gain < MIN_DEGREE_HOURS_GAIN:
        return RouteComparison(
            fastest=fastest, coolest=fastest, candidates=scored,
            worth_detour=False,
            reason=f"best alternative saves only {gain:.2f} °C·h — not worth the detour",
        )

    return RouteComparison(
        fastest=fastest, coolest=coolest, candidates=scored,
        worth_detour=True,
        reason=f"saves {gain:.2f} °C·h for {(coolest.duration_s - fastest.duration_s)/60:.0f} min extra",
    )

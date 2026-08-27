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

#: A detour must be priced over at least this much of itself before it can be
#: recommended. Unmeasured ground scores as zero exposure, so without a floor
#: the coolest-looking route is the one that left coverage. Candidates are also
#: held to the fastest route's own coverage, so a comparison where nothing is
#: well measured still returns the best of what there is rather than nothing.
MIN_COVERAGE = 0.5

#: Vertices kept per route when a comparison is sent to the dashboard. OSRM
#: returns 500–800 points for a city crossing, which is far more resolution
#: than a browser can show and more JSON than an SSE frame should carry.
MAX_DISPLAY_POINTS = 150


def _thin(coordinates: list[list[float]], limit: int) -> list[list[float]]:
    """Evenly drop vertices until at most ``limit`` remain.

    Endpoints are always kept, so a thinned line still starts and ends where
    the route does — a detour whose destination drifted would be worse than no
    line at all.
    """
    if limit < 2 or len(coordinates) <= limit:
        return list(coordinates)
    step = (len(coordinates) - 1) / (limit - 1)
    picked = [coordinates[round(i * step)] for i in range(limit)]
    picked[-1] = coordinates[-1]
    return picked


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
    #: Fraction of the sampled cells that returned a usable temperature. Below
    #: 1.0 the route leaves FortyGuard's coverage somewhere, and its score
    #: describes only the part we could measure — see :func:`score_candidate`.
    coverage: float = 0.0
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
            "coverage": round(self.coverage, 2),
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

    def to_map_payload(self, *, max_points: int = MAX_DISPLAY_POINTS) -> dict[str, Any]:
        """A compact version for the dispatch console's map.

        Deliberately not :meth:`to_dict`. A scored comparison carries every
        vertex of every candidate — measured at 565–812 points each — and this
        rides an SSE frame that a browser has to parse mid-demo. Only the two
        routes a dispatcher acts on are sent, thinned to ``max_points``, in
        Leaflet's ``[lat, lon]`` order rather than GeoJSON's.
        """
        def leg(candidate: RouteCandidate) -> dict[str, Any]:
            return {
                "label": candidate.label,
                "path": [[lat, lon] for lon, lat in _thin(candidate.coordinates, max_points)],
                "distance_km": round(candidate.distance_km, 2),
                "duration_min": round(candidate.duration_min, 1),
                "peak_c": candidate.peak_c,
                "degree_hours": (None if candidate.degree_hours is None
                                 else round(candidate.degree_hours, 2)),
                "coverage": round(candidate.coverage, 2),
            }

        payload: dict[str, Any] = {
            "worth_detour": self.worth_detour,
            "reason": self.reason,
            "fastest": leg(self.fastest),
        }
        # When no detour is worth it, fastest and coolest are the same object.
        # Sending it twice would draw one line over itself and imply a choice
        # the comparison did not actually offer.
        if self.worth_detour and self.coolest is not self.fastest:
            payload["coolest"] = leg(self.coolest)
        return payload


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

    Two things the arithmetic has to get right:

    **Which temperature.** Cells are scored on
    :attr:`~safety_rider.temperature_service.TemperatureReading.decision_celsius`
    — the same number the rider's own reply is banded on. Scoring on the daily
    peak instead let one conversation say "41.5 °C right now" and then price a
    route off a three-day-old 36 °C peak, which is the service disagreeing with
    itself about the same block.

    **The share denominator is cells sampled, not cells priced.** A route that
    leaves coverage part-way has fewer readings than cells; dividing by the
    readings would pour the whole trip's duration into the covered half and
    invent exposure that was never measured. Unpriced cells contribute zero
    instead, and :attr:`RouteCandidate.coverage` records how much of the route
    that leaves unknown so :func:`compare_routes` can refuse to recommend a
    detour that merely looks cool for lack of data.
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
        candidate.coverage = 0.0
        return candidate

    hours_total = candidate.duration_s / 3600.0
    share = hours_total / len(cells)

    temps = [r.decision_celsius for r in readings]
    candidate.degree_hours = sum(max(0.0, t - OSHA_HIGH_C) * share for t in temps)
    candidate.peak_c = round(max(temps), 1)
    candidate.mean_c = round(sum(temps) / len(temps), 1)
    candidate.cells_sampled = len(readings)
    candidate.coverage = len(readings) / len(cells)
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

    # Only consider detours a rider would actually accept — and only ones we
    # measured about as well as the route we are comparing them against.
    # An unpriced cell contributes zero degree-hours, so a candidate that
    # wanders out of coverage scores low for the reason we cannot see it. Left
    # unguarded, "cooler route found" would mean "route we know least about".
    acceptable = [
        c for c in scored
        if c.duration_s <= fastest.duration_s * MAX_DETOUR_RATIO
        and c.coverage >= min(fastest.coverage, MIN_COVERAGE) - 1e-9
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

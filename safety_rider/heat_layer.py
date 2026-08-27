"""Fetching, caching, and querying FortyGuard heat layers for a rider's location.

Why this module exists rather than calling the API straight from the risk
evaluator: a heatmap is billed **per request** and covers an **area**, not a
point. Calling one per inbound WhatsApp message would be both slow and
needlessly expensive, since two riders three blocks apart want the same layer.

So locations are snapped to a coarse grid, and one set of layers is fetched and
cached per (grid cell, date). Every rider in that cell is then answered from
local tiles with no further API traffic.

The cache format and the ``cached_or_live`` pattern deliberately mirror
``notebooks/use_cases/parcel_*.ipynb`` so a response captured here can be
inspected in a notebook and vice versa.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from .config import ROOT, settings

log = logging.getLogger(__name__)

# ── Coverage ───────────────────────────────────────────────────────────────
# The FortyGuard API serves the United States only; anything outside returns an
# error or empty result. Check locally so a rider abroad gets a clear message
# instead of a failed task and a burnt request.
_US_BBOXES = (
    # (min_lat, max_lat, min_lon, max_lon)
    (24.4, 49.4, -125.0, -66.9),    # contiguous 48
    (51.0, 71.6, -180.0, -129.0),   # Alaska
    (18.8, 22.3, -160.3, -154.7),   # Hawaii
)


def is_in_coverage(latitude: float, longitude: float) -> bool:
    """True if the point falls inside the API's U.S. coverage area."""
    return any(
        lo_lat <= latitude <= hi_lat and lo_lon <= longitude <= hi_lon
        for lo_lat, hi_lat, lo_lon, hi_lon in _US_BBOXES
    )


# ── Geometry helpers ───────────────────────────────────────────────────────

_M_PER_DEG_LAT = 111_320.0


def _deg_offsets(latitude: float, metres: float) -> tuple[float, float]:
    """Convert a distance in metres to (lat_degrees, lon_degrees) at this latitude."""
    d_lat = metres / _M_PER_DEG_LAT
    # Longitude degrees shrink towards the poles.
    d_lon = metres / (_M_PER_DEG_LAT * max(math.cos(math.radians(latitude)), 0.01))
    return d_lat, d_lon


def build_aoi(latitude: float, longitude: float, half_side_m: float) -> dict[str, Any]:
    """A square GeoJSON FeatureCollection centred on the point.

    Note the coordinate order: GeoJSON is ``[longitude, latitude]``, the reverse
    of how the point arrives from WhatsApp.
    """
    d_lat, d_lon = _deg_offsets(latitude, half_side_m)
    min_lat, max_lat = latitude - d_lat, latitude + d_lat
    min_lon, max_lon = longitude - d_lon, longitude + d_lon
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [min_lon, min_lat],
                    [max_lon, min_lat],
                    [max_lon, max_lat],
                    [min_lon, max_lat],
                    [min_lon, min_lat],
                ]],
            },
        }],
    }


def grid_key(latitude: float, longitude: float) -> tuple[float, float]:
    """Snap a point to the shared-cache grid, returning the cell's centre.

    ``SAFETY_RIDER_GRID_DEG`` controls the cell size (default 0.05° ≈ 5.5 km).
    The cell must be comfortably smaller than the AOI, or a rider near its edge
    would sit outside the layer fetched for its centre.
    """
    step = settings.heat_grid_deg
    return (
        round(math.floor(latitude / step) * step + step / 2, 5),
        round(math.floor(longitude / step) * step + step / 2, 5),
    )


# ── Cached layer access ────────────────────────────────────────────────────

#: One lock per cache key, so two riders arriving in the same cell at the same
#: moment produce one billed API call rather than two.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def cache_dir() -> Path:
    path = ROOT / "data" / "heatmaps"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cached_or_live(
    cache_path: Path,
    label: str,
    fetch: Callable[..., dict[str, Any]] | None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return a cached API response, fetching and storing it on a miss.

    Mirrors the ``cached_or_live()`` helper in the parcel notebooks: read the
    file if it is there, otherwise call the API once and write the result down.
    ``settings.heat_refresh`` forces a live call and re-bills.
    """
    with _lock_for(str(cache_path)):
        if cache_path.exists() and not settings.heat_refresh:
            log.info("Heat layer cache hit: %s", cache_path.name)
            return json.loads(cache_path.read_text())

        if fetch is None:
            raise RuntimeError(
                f"No cached {label} at {cache_path} and no API client available."
            )

        log.info("Heat layer cache miss — calling FortyGuard for %s", label)
        response = fetch(**kwargs)
        # _submit_and_wait returns {"activity_id": ..., "result": ...}; the
        # result is the part worth caching and the part notebooks read.
        result = response.get("result", response) if isinstance(response, dict) else response
        cache_path.write_text(json.dumps(result))
        return result


@dataclass
class HeatLayer:
    """A fetched heatmap, queryable by point.

    Tiles are indexed with an STRtree so a lookup is O(log n) rather than a scan
    over several thousand polygons on every message.
    """

    tiles: list[Any]
    properties: list[dict[str, Any]]
    stats: dict[str, Any]
    _index: STRtree

    @classmethod
    def from_response(cls, response: dict[str, Any]) -> "HeatLayer":
        map_data = response.get("map_data") or {}
        features = map_data.get("features", []) if isinstance(map_data, dict) else []

        geometries: list[Any] = []
        properties: list[dict[str, Any]] = []
        for feature in features:
            geometry = feature.get("geometry")
            if not geometry:
                continue
            try:
                geometries.append(shape(geometry))
            except Exception:  # malformed tile — skip rather than fail the lookup
                continue
            properties.append(feature.get("properties") or {})

        if not geometries:
            raise ValueError("Heat layer response contained no usable tiles.")

        return cls(
            tiles=geometries,
            properties=properties,
            stats=response.get("stats_data") or {},
            _index=STRtree(geometries),
        )

    def lookup(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """Properties of the tile containing the point, or the nearest one.

        A rider standing just outside the AOI edge, or on a tile boundary, still
        gets an answer — falling back to nearest is far better than silence, and
        at 100 m granularity the nearest tile is a defensible read.
        """
        point = Point(longitude, latitude)  # GeoJSON order

        for idx in self._index.query(point):
            if self.tiles[idx].contains(point):
                return self.properties[idx]

        nearest = self._index.nearest(point)
        if nearest is None:
            return None
        idx = int(nearest)
        # Guard against a point far outside the AOI silently matching an edge tile.
        if self.tiles[idx].distance(point) > 0.02:  # ~2 km in degrees
            return None
        return self.properties[idx]




def _is_partial_day(features: list[dict[str, Any]]) -> bool:
    """True when a daily layer is really one ingested hour wearing a day's name.

    A complete day spans a real diurnal range, so a tile's min and max differ.
    A partial one carries the same number in both on every tile. Sampling a few
    tiles is enough — the collapse is total, not patchy — and beats walking ten
    thousand of them on every dashboard request.
    """
    checked = 0
    for feature in features:
        props = feature.get("properties") or {}
        low, high = props.get("min_temperature"), props.get("max_temperature")
        if low is None or high is None:
            continue
        try:
            if abs(float(high) - float(low)) >= 0.01:
                return False
        except (TypeError, ValueError):
            continue
        checked += 1
        if checked >= 25:
            break
    return checked > 0


def cached_tcm_geojson(
    latitude: float,
    longitude: float,
    *,
    max_days_back: int | None = None,
) -> dict[str, Any] | None:
    """The cached daily heat tiles for this cell, trimmed for the browser.

    **Reads the cache only — never calls the API.** The dashboard fetches this
    on a button press and on every rider that appears, so a version that could
    bill would turn an idle browser tab into a spend. A cell nobody has queried
    simply has no overlay, which is the honest answer.

    Returns a GeoJSON FeatureCollection whose features carry a single ``t``
    property (the tile's peak °C), plus the range and the date the tiles
    describe. ``None`` when nothing is cached for this cell.

    The trimming matters: the raw response is ~930 KB for 10,000 tiles and
    carries four properties per tile plus full float precision. Coordinates are
    rounded to five decimals — about a metre, well under the 60-100 m tile — and
    only the peak survives, which is what the colour ramp reads.
    """
    cell_lat, cell_lon = grid_key(latitude, longitude)
    directory = cache_dir()
    horizon = max_days_back if max_days_back is not None else (
        settings.heat_days_back + settings.heat_backfill_days
    )

    today = date.today()
    for step in range(horizon + 1):
        study_date = (today - timedelta(days=step)).isoformat()
        path = directory / f"heatmap_rider_{cell_lat:.5f}_{cell_lon:.5f}_{study_date}_tcm.json"
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            log.warning("Cached layer %s is unreadable — skipping.", path.name)
            continue

        features_in = ((raw.get("map_data") or {}).get("features") or [])
        if _is_partial_day(features_in):
            # The same layer the verdict path refuses: a day the catalog has
            # not finished ingesting returns one hour's snapshot with
            # min == mean == max on every tile. Drawing it would paint an
            # hour's noise as a day's heat gradient, in colour, on the screen
            # a dispatcher trusts — a prettier version of the exact mistake
            # temperature_service exists to avoid. Walk back instead.
            log.info("Cached layer for %s is a partial day — not drawing it.", study_date)
            continue

        features: list[dict[str, Any]] = []
        low = high = None
        for feature in features_in:
            geometry = feature.get("geometry") or {}
            props = feature.get("properties") or {}
            peak = props.get("max_temperature")
            if geometry.get("type") != "Polygon" or peak is None:
                continue
            try:
                peak = round(float(peak), 2)
            except (TypeError, ValueError):
                continue
            rings = [[[round(float(x), 5), round(float(y), 5)] for x, y in ring]
                     for ring in geometry.get("coordinates", [])]
            if not rings:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": rings},
                "properties": {"t": peak},
            })
            low = peak if low is None else min(low, peak)
            high = peak if high is None else max(high, peak)

        if not features:
            continue

        return {
            "type": "FeatureCollection",
            "features": features,
            "observed_date": study_date,
            "cell": [cell_lat, cell_lon],
            "min_c": low,
            "max_c": high,
            "tiles": len(features),
        }

    return None

def fetch_layers(
    client: Any,
    latitude: float,
    longitude: float,
    study_date: str | None = None,
    threshold_c: float = 32.2,
) -> tuple[HeatLayer, HeatLayer | None]:
    """Fetch (or load from cache) the ``tcm`` and ``exceedance`` layers.

    Returns ``(tcm_layer, exceedance_layer)``. The exceedance layer is optional:
    it is the more useful of the two but also the more likely to fail on a given
    date/AOI, and a peak-temperature-only answer still beats no answer.
    """
    study_date = study_date or date.today().isoformat()
    cell_lat, cell_lon = grid_key(latitude, longitude)
    aoi = build_aoi(cell_lat, cell_lon, settings.heat_aoi_half_side_m)
    slug = f"rider_{cell_lat:.5f}_{cell_lon:.5f}_{study_date}"
    directory = cache_dir()

    # Layer 1 — daily snapshot. filter_type=3 aggregates the whole day, so each
    # tile carries min / mean / max temperature in °C.
    tcm_response = cached_or_live(
        directory / f"heatmap_{slug}_tcm.json",
        f"tcm heatmap {study_date}",
        getattr(client, "create_heatmap", None) if client else None,
        polygon_aoi=aoi,
        start_date=study_date,
        filter_type=3,
        granularity=settings.heat_granularity_m,
        analytic_type="tcm",
        verbose=False,
        timeout=settings.heat_timeout_s,
    )
    tcm_layer = HeatLayer.from_response(tcm_response)

    # Layer 2 — exposure duration. This is the metric that actually separates
    # one location from another below city scale; the snapshot above is nearly
    # flat over a few km. filter_type=2 spans the hours of a single day, so the
    # value is "hours above threshold today".
    exceedance_layer: HeatLayer | None = None
    try:
        exceedance_response = cached_or_live(
            directory / f"heatmap_{slug}_exceedance_{threshold_c:g}.json",
            f"exceedance heatmap {study_date}",
            getattr(client, "create_heatmap", None) if client else None,
            polygon_aoi=aoi,
            start_date=study_date,
            start_time="00:00",
            end_time="23:00",
            filter_type=2,
            granularity=settings.heat_granularity_m,
            analytic_type="exceedance",
            threshold=threshold_c,
            direction="above",
            verbose=False,
            timeout=settings.heat_timeout_s,
        )
        exceedance_layer = HeatLayer.from_response(exceedance_response)
    except Exception as exc:  # noqa: BLE001 — degrade to snapshot-only
        log.warning("Exceedance layer unavailable (%s) — continuing without it.", exc)

    return tcm_layer, exceedance_layer


def fetch_hourly_tcm(
    client: Any,
    latitude: float,
    longitude: float,
    study_date: str,
    hour: int,
    *,
    cache_only: bool = True,
    timeout_s: float | None = None,
) -> HeatLayer | None:
    """The ``tcm`` layer for a single elapsed hour, or ``None`` if it has none.

    ``filter_type=1`` returns one temperature per tile for one hour, so each
    tile's ``min``/``average``/``max`` are the same number by construction. That
    is *not* the partial-day signature :func:`_read_day` screens for — do not
    reuse that check here or every valid hour is rejected.

    An hour the catalog has not reached comes back as a well-formed but **empty**
    FeatureCollection rather than an error. Measured 2026-08-24 at 12:47 UTC:
    ``12:00`` returned 360 tiles spanning 28.73-30.93 °C, while ``18:00`` six
    hours ahead returned ``{"features": []}`` with ``n_cells: 0`` and no error
    attached. So emptiness is the signal, and it has to be caught by shape.

    **``cache_only`` defaults to True, and the rider path must never turn it
    off.** These endpoints are submit-then-poll and slow: measured 2026-08-24, a
    1 km AOI of 380 tiles took **219 seconds**. Nobody waits that long on
    WhatsApp — and worse, a client-side timeout abandons the poll but not the
    job, so every attempt bills whether or not anyone reads the answer. Warming
    the cache out-of-band (see :mod:`safety_rider.warm`) is what makes the
    nowcast affordable and fast; on the rider path a cold cell simply has no
    nowcast, and the daily reading answers.

    ``timeout_s`` defaults to the rider-path budget, which is deliberately short.
    A caller that submits (``cache_only=False``) must raise it, because a timeout
    there is not a cheap failure: the job is billed at submission and abandoning
    the poll discards a paid-for answer. See ``settings.heat_warm_timeout_s``.
    """
    cell_lat, cell_lon = grid_key(latitude, longitude)
    aoi = build_aoi(cell_lat, cell_lon, settings.heat_aoi_half_side_m)
    slug = f"rider_{cell_lat:.5f}_{cell_lon:.5f}_{study_date}_{hour:02d}00"

    try:
        response = cached_or_live(
            cache_dir() / f"heatmap_{slug}_tcm.json",
            f"hourly tcm heatmap {study_date} {hour:02d}:00",
            # None means "cache or nothing" — cached_or_live raises on a miss
            # rather than submitting, and the caller degrades to daily.
            None if cache_only else
            (getattr(client, "create_heatmap", None) if client else None),
            polygon_aoi=aoi,
            start_date=study_date,
            start_time=f"{hour:02d}:00",
            filter_type=1,
            granularity=settings.heat_granularity_m,
            analytic_type="tcm",
            verbose=False,
            timeout=settings.heat_timeout_s if timeout_s is None else timeout_s,
        )
        return HeatLayer.from_response(response)
    except Exception as exc:  # noqa: BLE001 — the nowcast is an enhancement
        log.info("Hourly layer %s %02d:00 unavailable (%s).", study_date, hour, exc)
        return None

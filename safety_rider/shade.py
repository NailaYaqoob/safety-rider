"""Canopy cover per grid cell, from FortyGuard satellite segmentation.

Air temperature is what the heat layer measures, and it is not the whole of
what a rider feels. Two streets can read the same 2 m air temperature while one
is under continuous tree canopy and the other is open asphalt in direct sun;
the radiant load on a body differs enormously between them. ``POST /v1/satellite``
classifies land cover at a point, so the canopy share of a cell is available —
and that is the part of "shade" a satellite can actually see.

**What this is not.** It is not a shadow model. It does not know the sun's
angle, the time of day, or which side of a building a rider is on, and it
cannot tell a tree that overhangs the carriageway from one set back behind a
fence. It is the fraction of a ~100 m tile that is canopy, which is a decent
proxy for "this block is shaded" and a poor one for "this exact metre is".

**Costs and caching.** Segmentation is a Premium, billed, submit-then-poll
endpoint on the same queue as the heatmaps, so it is far too slow for a
WhatsApp reply. Two things make it affordable anyway:

* Land cover does not change hour to hour, or week to week. Unlike a heat
  layer, the cache has no date in its key and effectively never expires — a
  cell is paid for once.
* The rider path reads it **cache-only**, exactly like the nowcast. A cold cell
  contributes no shade information and routing falls back to temperature
  alone, which is the behaviour that existed before this module.

The spend happens out of band in :mod:`safety_rider.warm`, bounded by service
area rather than by traffic.

**Shade never changes a safety verdict.** It is an input to *route ranking*
only. The band a rider is told — Safe, Warning, Danger — is decided on measured
air temperature at their own position, and nothing here can soften it. A model
that let a leafy street talk a 41 °C reading down into "Warning" would be
inventing safety out of an assumption, which is the one failure this service
cannot afford.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from .config import settings
from .heat_layer import _lock_for, cache_dir, grid_key, is_in_coverage

log = logging.getLogger(__name__)

#: Substrings that mark a land-cover class as overhead canopy — the thing that
#: actually stands between a rider and the sun.
#:
#: Matched as substrings rather than compared to a fixed list, because the API
#: returns whatever class names its model uses and those are not pinned in any
#: documentation this repo can see. A schema guess that silently matched
#: nothing would read as "no shade anywhere" — indistinguishable from a real
#: answer — so :func:`shade_fraction` logs the classes it saw when none match.
_CANOPY_HINTS = ("tree", "canopy", "forest", "woodland")

#: Ground-level greenery. Cooler underfoot and it does lower the surrounding
#: air, but it puts nothing between a rider and the sun, so it earns partial
#: credit rather than full.
_GREEN_HINTS = ("vegetation", "grass", "shrub", "park", "garden", "crop", "green")

#: Surfaces that are recognisably *not* shade. Listed so the module can tell
#: "this block is genuinely bare asphalt" from "this response used class names
#: I do not know" — the first is the most useful signal shade routing has, and
#: reporting it as unknown would throw away exactly the cell a rider should be
#: routed around.
_BUILT_HINTS = (
    "building", "roof", "road", "pavement", "paving", "asphalt", "concrete",
    "impervious", "parking", "bare", "soil", "sand", "rock", "water", "pool",
    "railway", "car", "vehicle", "sidewalk", "footpath",
)

#: How much of a green (non-canopy) class counts toward shade.
_GREEN_CREDIT = 0.35


def _classify(segments: dict[str, Any]) -> tuple[float, float, list[str]]:
    """Split segmentation percentages into (canopy, green, unmatched names).

    Percentages arrive as 0–100 in the responses this repo has seen, but a
    normalised 0–1 payload would silently produce a hundredfold error in the
    wrong direction — a fully paved block reported as fully shaded. So the
    scale is inferred from the total rather than assumed.
    """
    total = 0.0
    canopy = 0.0
    green = 0.0
    unmatched: list[str] = []

    for name, value in segments.items():
        try:
            share = float(value)
        except (TypeError, ValueError):
            continue
        if share < 0:
            continue
        total += share
        lowered = str(name).lower()
        if any(hint in lowered for hint in _CANOPY_HINTS):
            canopy += share
        elif any(hint in lowered for hint in _GREEN_HINTS):
            green += share
        elif not any(hint in lowered for hint in _BUILT_HINTS):
            unmatched.append(str(name))

    if total <= 0:
        return 0.0, 0.0, unmatched
    return canopy / total, green / total, unmatched


def shade_fraction_from_result(result: dict[str, Any]) -> float | None:
    """Canopy-weighted shade share (0–1) from a segmentation result, or None."""
    segments = ((result or {}).get("segmentation") or {}).get("segments") or {}
    if not isinstance(segments, dict) or not segments:
        return None

    canopy, green, unmatched = _classify(segments)
    if canopy == 0.0 and green == 0.0 and unmatched:
        # Found no greenery AND met class names this module does not recognise.
        # Far more likely a vocabulary it has not seen than a place with no
        # vegetation at all, so the honest answer is "unknown" rather than a
        # confident zero. When every class IS recognised and none is green,
        # zero is a real measurement — bare asphalt — and falls through below.
        log.warning(
            "No canopy or vegetation class recognised in segmentation; "
            "unrecognised classes were %s. Treating shade as unknown.",
            sorted(unmatched)[:12],
        )
        return None

    return round(min(1.0, canopy + green * _GREEN_CREDIT), 4)


def _cache_path(latitude: float, longitude: float) -> Path:
    cell_lat, cell_lon = grid_key(latitude, longitude)
    directory = cache_dir().parent / "segmentation"
    directory.mkdir(parents=True, exist_ok=True)
    # No date in the key: land cover is stable, so a cell is billed once and
    # then answered from disk forever.
    return directory / f"shade_{cell_lat:.5f}_{cell_lon:.5f}.json"


def shade_fraction(
    latitude: float,
    longitude: float,
    *,
    client: Any = None,
    cache_only: bool = True,
) -> float | None:
    """Shade share of this point's grid cell, or ``None`` when unknown.

    ``cache_only=True`` (the default, and what the rider path uses) never calls
    the API: a cell nobody has warmed simply has no shade information, and
    routing falls back to temperature alone. Pass ``cache_only=False`` with a
    client only from :mod:`safety_rider.warm`, where minutes of polling cost
    nothing.

    Never raises. Shade is an enhancement to route ranking, and a failure here
    must degrade to "unknown", never to a missing route.
    """
    if not settings.shade_routing:
        return None
    if not is_in_coverage(latitude, longitude):
        return None

    path = _cache_path(latitude, longitude)

    with _lock_for(str(path)):
        if path.exists():
            try:
                cached = json.loads(path.read_text())
                value = cached.get("shade_fraction")
                return None if value is None else float(value)
            except (OSError, ValueError, TypeError):
                log.warning("Cached shade at %s is unreadable — refetching.", path.name)

        if cache_only or client is None:
            return None

        try:
            response = client.satellite_segmentation(
                latitude=latitude,
                longitude=longitude,
                start_date=date.today().isoformat(),
                filter_type=3,
                granularity=settings.heat_granularity_m,
                timeout=settings.heat_warm_timeout_s,
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001 — see the docstring.
            log.warning("Satellite segmentation failed for %.4f,%.4f: %s",
                        latitude, longitude, exc)
            return None

        result = response.get("result", response) if isinstance(response, dict) else {}
        fraction = shade_fraction_from_result(result)
        if fraction is None:
            return None

        cell_lat, cell_lon = grid_key(latitude, longitude)
        path.write_text(json.dumps({
            "cell": [cell_lat, cell_lon],
            "shade_fraction": fraction,
            "image_year": result.get("image_year"),
            "segments": ((result.get("segmentation") or {}).get("segments") or {}),
        }))
        log.info("Cached shade for cell %.5f,%.5f: %.0f%% canopy-weighted.",
                 cell_lat, cell_lon, fraction * 100)
        return fraction


def describe(fraction: float | None) -> str:
    """A phrase a rider can read. Deliberately coarse — the data is a proxy."""
    if fraction is None:
        return "shade unknown"
    if fraction >= 0.45:
        return "mostly shaded"
    if fraction >= 0.25:
        return "partly shaded"
    if fraction >= 0.10:
        return "patchy shade"
    return "little shade"

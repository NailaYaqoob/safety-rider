"""Warm the hourly nowcast cache for the cells a fleet actually operates in.

Why this exists as a separate step rather than happening on demand: the heatmap
endpoints are submit-then-poll and slow. Measured 2026-08-24, a 1 km AOI of 380
tiles took **219 seconds**. That cannot sit inside a WhatsApp reply, and a
client-side timeout does not cancel the billed job — it only stops you reading
the answer, so an on-demand retry loop pays repeatedly for results nobody sees.

So the rider path reads the hourly layer only if it is already warm
(``fetch_hourly_tcm(..., cache_only=True)``) and falls back to the daily reading
otherwise. This module is the other half: it submits those requests ahead of
demand, where four minutes costs nothing.

Cost is per (grid cell, hour), so warming is bounded by the *service area*, not
by traffic — one request per active cell per hour whether one rider pins there
or fifty.

    python -m safety_rider.warm 33.4484 -112.0740
    python -m safety_rider.warm 33.4484 -112.0740 --hours 3

In production this belongs on a schedule, one run per hour per service area.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from fortyguard import FortyGuardClient

from . import heat_layer
from .config import settings

log = logging.getLogger(__name__)


def warm_cell(latitude: float, longitude: float, hours: int = 1) -> int:
    """Fetch and cache the last ``hours`` hourly layers for this point's cell.

    Returns how many hours are warm afterwards. Never raises: a warmer that
    crashes a scheduler is worse than one that reports a miss.
    """
    if not heat_layer.is_in_coverage(latitude, longitude):
        log.error("%.4f,%.4f is outside FortyGuard coverage — nothing to warm.",
                  latitude, longitude)
        return 0

    client = FortyGuardClient()
    now = datetime.now(tz=timezone.utc)
    warmed = 0

    for back in range(hours):
        moment = now - timedelta(hours=back)
        stamp = f"{moment.date().isoformat()} {moment.hour:02d}:00 UTC"
        layer = heat_layer.fetch_hourly_tcm(
            client, latitude, longitude,
            study_date=moment.date().isoformat(), hour=moment.hour,
            cache_only=False,
        )
        if layer is None:
            # Expected for an hour the catalog has not reached yet.
            log.warning("%s — no data (not ingested yet).", stamp)
            continue

        tile = layer.lookup(latitude, longitude)
        value = tile.get("max_temperature") if tile else None
        log.info("%s — warm (%s °C at this tile).", stamp,
                 f"{float(value):.2f}" if value is not None else "no tile")
        warmed += 1

    return warmed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--hours", type=int, default=1,
                        help="how many hours back to warm (default 1). "
                             "Each is a billed request.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    if not (settings.heat_live and settings.fortyguard_api_key):
        log.error("Live heat is disabled or no API key is set — nothing to do.")
        return 2

    cell = heat_layer.grid_key(args.latitude, args.longitude)
    log.info("Warming cell %.5f,%.5f (%d hour(s))…", cell[0], cell[1], args.hours)
    warmed = warm_cell(args.latitude, args.longitude, hours=args.hours)
    log.info("Done — %d of %d hour(s) warm.", warmed, args.hours)
    return 0 if warmed else 1


if __name__ == "__main__":
    sys.exit(main())

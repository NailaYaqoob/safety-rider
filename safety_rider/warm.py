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
import json
import logging
import sys
import time
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
            # The rider-path budget (120 s) is wrong here and expensively so:
            # submission bills, so giving up early throws away an answer already
            # paid for. Measured 2026-08-24, the same request took 219 s once and
            # was still queued past 120 s three hours later.
            timeout_s=settings.heat_warm_timeout_s,
        )
        if layer is None:
            # Two very different failures land here and the operator needs to
            # tell them apart: an hour the catalog has not reached is free to
            # retry later, while a timeout has already been billed and its
            # activity id is the only way back to the result.
            log.warning("%s — not warmed (no data yet, or the poll gave up; "
                        "see the reason logged above).", stamp)
            continue

        tile = layer.lookup(latitude, longitude)
        value = tile.get("max_temperature") if tile else None
        log.info("%s — warm (%s °C at this tile).", stamp,
                 f"{float(value):.2f}" if value is not None else "no tile")
        warmed += 1

    return warmed


def resume_activity(
    activity_id: str,
    latitude: float,
    longitude: float,
    study_date: str,
    hour: int,
    poll_timeout_s: float = 1500.0,
) -> bool:
    """Adopt an already-submitted activity into the cache. Returns True if cached.

    Submission is what bills, so a poll that gives up does not save money — it
    only discards an answer already bought. The activity id outlives the client
    that abandoned it, and the status endpoint is free to read, so a lost job is
    recoverable for as long as the API keeps the result::

        python -m safety_rider.warm 33.4484 -112.0740 \
            --resume d99b236e-337a-4706-8f54-7ac2618e7498 --hour 15

    Recovered a 9,945-tile Phoenix layer on 2026-08-24 after the warmer timed
    out at 120 s, at no additional cost.
    """
    from fortyguard import FortyGuardClient

    cell_lat, cell_lon = heat_layer.grid_key(latitude, longitude)
    slug = f"rider_{cell_lat:.5f}_{cell_lon:.5f}_{study_date}_{hour:02d}00"
    path = heat_layer.cache_dir() / f"heatmap_{slug}_tcm.json"
    if path.exists():
        log.info("Already cached: %s", path.name)
        return True

    client = FortyGuardClient()
    deadline = time.monotonic() + poll_timeout_s
    while True:
        data = client.get_status(activity_id)
        status = str(data.get("status", "")).lower()
        if status in {"completed", "success", "succeeded", "done"}:
            result = data.get("result", data)
            features = ((result.get("map_data") or {}).get("features") or [])
            if not features:
                # An hour the catalog has not reached returns a well-formed but
                # empty layer. Caching it would pin that emptiness for the hour.
                log.warning("%s %02d:00 completed but is empty — not cached.",
                            study_date, hour)
                return False
            path.write_text(json.dumps(result))
            log.info("Recovered %d tiles into %s", len(features), path.name)
            return True
        if status in {"failed", "error", "cancelled"}:
            log.error("Activity %s %s: %s", activity_id, status,
                      data.get("message") or "")
            return False
        if time.monotonic() >= deadline:
            log.error("Activity %s still '%s' after %.0fs — still recoverable, "
                      "run --resume again later.", activity_id, status, poll_timeout_s)
            return False
        time.sleep(15)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--hours", type=int, default=1,
                        help="how many hours back to warm (default 1). "
                             "Each is a billed request.")
    parser.add_argument("--resume", metavar="ACTIVITY_ID",
                        help="adopt an already-submitted activity into the cache "
                             "instead of submitting a new one. Free — the job was "
                             "billed at submission.")
    parser.add_argument("--date", help="UTC date for --resume (default: today).")
    parser.add_argument("--hour", type=int,
                        help="UTC hour for --resume (default: the current hour).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    if not (settings.heat_live and settings.fortyguard_api_key):
        log.error("Live heat is disabled or no API key is set — nothing to do.")
        return 2

    cell = heat_layer.grid_key(args.latitude, args.longitude)

    if args.resume:
        now = datetime.now(tz=timezone.utc)
        ok = resume_activity(
            args.resume, args.latitude, args.longitude,
            study_date=args.date or now.date().isoformat(),
            hour=now.hour if args.hour is None else args.hour,
        )
        return 0 if ok else 1

    log.info("Warming cell %.5f,%.5f (%d hour(s))…", cell[0], cell[1], args.hours)
    warmed = warm_cell(args.latitude, args.longitude, hours=args.hours)
    log.info("Done — %d of %d hour(s) warm.", warmed, args.hours)
    return 0 if warmed else 1


if __name__ == "__main__":
    sys.exit(main())

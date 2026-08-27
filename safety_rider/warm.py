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

In production this runs on a schedule — :func:`run_scheduler`, started by the
app when ``SAFETY_RIDER_WARM_CELLS`` names the ground a fleet covers.

**The schedule lives inside the service on purpose.** A warmed layer is a file
in the heat cache directory and the rider path reads that same directory, so a
separate cron container or a CI workflow would warm its own filesystem and the
service would never see it. Whatever warms this cache has to share the volume
with the process reading it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from fortyguard import FortyGuardClient

from . import heat_layer
from .config import settings
from .shade import shade_fraction

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


# ─────────────────────────────────────────────────────────────── scheduler


def warm_shade(latitude: float, longitude: float) -> float | None:
    """Pay for this cell's satellite segmentation once, so routing can use it.

    Land cover does not change hour to hour, so unlike the hourly layers this
    is billed once per cell and then answered from disk forever — an already
    cached cell costs nothing and returns immediately. Never raises: shade is
    an enhancement to route ranking, and losing it must not cost a warm pass.
    """
    if not settings.shade_routing:
        return None
    try:
        client = FortyGuardClient()
        return shade_fraction(latitude, longitude, client=client, cache_only=False)
    except Exception:  # noqa: BLE001 — see the docstring.
        log.exception("Shade warm failed for %.4f,%.4f; continuing.", latitude, longitude)
        return None


async def warm_once(cells: list[tuple[float, float]], hours: int) -> int:
    """One pass over the service area. Returns how many hours came back warm.

    Cells are warmed **one at a time**, not concurrently. The endpoint is a
    queue and the wait is queue depth rather than tile count, so firing every
    cell at once lengthens each one and bills them all simultaneously — the
    opposite of what an out-of-band warmer is for.
    """
    warmed = 0
    for latitude, longitude in cells:
        try:
            # warm_cell blocks for minutes at a time. Off the event loop, or a
            # single pass would stall every rider waiting on a reply.
            warmed += await asyncio.to_thread(warm_cell, latitude, longitude, hours)
            # Segmentation is billed once per cell and cached with no expiry,
            # so after the first pass over a service area this is a disk read.
            fraction = await asyncio.to_thread(warm_shade, latitude, longitude)
            if fraction is not None:
                log.info("Shade for %.4f,%.4f: %.0f%% canopy-weighted.",
                         latitude, longitude, fraction * 100)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a scheduler must outlive one bad cell.
            log.exception("Warming %.4f,%.4f failed; continuing.", latitude, longitude)
    return warmed


async def run_scheduler() -> None:
    """Warm the configured cells now, then once per interval, forever.

    Never raises. A warmer that takes the service down with it would trade a
    missing nowcast — which the daily reading already covers — for missing
    safety verdicts, and the whole point of reading the hourly layer cache-only
    on the rider path is that its absence costs nothing.

    Cancelled cleanly at shutdown; :exc:`asyncio.CancelledError` is re-raised
    rather than swallowed so the event loop can actually finish closing.
    """
    cells = settings.warm_cells
    if not cells:
        return

    log.info(
        "Nowcast warmer scheduled: %d cell(s), %d hour(s) each, every %.0f min. "
        "Each cell-hour is one billed FortyGuard request.",
        len(cells), settings.warm_hours, settings.warm_interval_s / 60,
    )

    while True:
        started = time.monotonic()
        try:
            warmed = await warm_once(cells, settings.warm_hours)
            log.info("Warm pass complete: %d of %d cell-hour(s) warm.",
                     warmed, len(cells) * settings.warm_hours)
        except asyncio.CancelledError:
            log.info("Nowcast warmer stopped.")
            raise
        except Exception:  # noqa: BLE001 — see the docstring.
            log.exception("Warm pass failed; will try again next interval.")

        # Measured from the START of the pass, not the end. A pass can take
        # minutes, and sleeping a full interval afterwards would let the
        # schedule drift past the hour the cache is keyed on.
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(60.0, settings.warm_interval_s - elapsed))


def scheduler_should_run() -> tuple[bool, str]:
    """Whether to start the warmer, and the reason either way (for the log)."""
    if not settings.warm_cells_raw:
        return False, "SAFETY_RIDER_WARM_CELLS is not set"
    if not settings.nowcast:
        return False, "the nowcast is disabled (SAFETY_RIDER_NOWCAST=0)"
    if not (settings.heat_live and settings.fortyguard_api_key):
        return False, "live heat is disabled or no API key is set"
    if not settings.warm_cells:
        return False, "SAFETY_RIDER_WARM_CELLS held no usable coordinates"
    return True, f"{len(settings.warm_cells)} cell(s) configured"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--hours", type=int, default=1,
                        help="how many hours back to warm (default 1). "
                             "Each is a billed request.")
    parser.add_argument("--shade-only", action="store_true",
                        help="fetch only the satellite segmentation for this cell "
                             "(billed once, then cached forever) and skip the "
                             "hourly heat layers.")
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

    if args.shade_only:
        log.info("Fetching segmentation for cell %.5f,%.5f…", cell[0], cell[1])
        fraction = warm_shade(args.latitude, args.longitude)
        if fraction is None:
            log.error("No shade data for this cell.")
            return 1
        log.info("Done — %.0f%% canopy-weighted shade.", fraction * 100)
        return 0

    log.info("Warming cell %.5f,%.5f (%d hour(s))…", cell[0], cell[1], args.hours)
    warmed = warm_cell(args.latitude, args.longitude, hours=args.hours)
    fraction = warm_shade(args.latitude, args.longitude)
    if fraction is not None:
        log.info("Shade: %.0f%% canopy-weighted.", fraction * 100)
    log.info("Done — %d of %d hour(s) warm.", warmed, args.hours)
    return 0 if warmed else 1


if __name__ == "__main__":
    sys.exit(main())

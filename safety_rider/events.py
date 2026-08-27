"""In-process event hub and rider registry backing the live dashboard.

Two things live here:

* :class:`RiderState` — where each rider is and how hot it is there. The map
  draws from this.
* :class:`Hub` — a fan-out bus. The webhook pipeline publishes one event per
  evaluation; every open dashboard receives it over Server-Sent Events.

**Scope: one process.** Subscribers are :class:`asyncio.Queue` objects held in
memory, so this works for a single uvicorn worker and breaks silently the
moment you run two — the rider who hits worker B never appears on a dashboard
streaming from worker A. That is the right trade for a hackathon demo and the
wrong one for production; swap the hub for Redis pub/sub when you scale out.
The dedup cache in :mod:`safety_rider.whatsapp.webhook` has the same limit and
should move at the same time.

Slow or dead subscribers are dropped rather than allowed to apply backpressure:
a browser tab that stops reading must never be able to stall the pipeline that
answers riders.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from .config import ROOT

log = logging.getLogger(__name__)

#: Where the rider registry survives a restart. Under ``data/`` so it lands on
#: the same mounted volume as the heat cache in production; on an ephemeral
#: filesystem it simply starts empty, which is the old behaviour.
REGISTRY_PATH = Path(os.getenv("SAFETY_RIDER_REGISTRY_PATH") or (ROOT / "data" / "riders.json"))

#: How long a remembered position stays usable. This file holds where people
#: were, so it is not kept a minute longer than it is useful: 24 hours is also
#: Meta's customer-service window, past which we cannot send a free-form reply
#: anyway, so an older entry could not be acted on even if we kept it.
REGISTRY_TTL = timedelta(hours=24)

#: How many past events a newly-opened dashboard is replayed. Enough that a
#: judge opening the page mid-demo sees the story so far, not an empty panel.
HISTORY_LIMIT = 100

#: Dropped if a subscriber falls this far behind — see the module docstring.
QUEUE_MAXSIZE = 200


def mask_number(number: str) -> str:
    """``14155550123`` → ``+14155****123``.

    The dashboard is built to be screen-shared in a pitch video, so a rider's
    full number must not be the default. Set ``SAFETY_RIDER_DASHBOARD_UNMASK=1``
    to show it in full when you are demoing to yourself.
    """
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        # Not a phone number at all (e.g. the "demo" placeholder). Pass it
        # through rather than emitting a bare "+".
        return number
    if len(digits) <= 8:
        return "+" + digits
    return f"+{digits[:5]}****{digits[-3:]}"


@dataclass
class RiderState:
    """One rider's latest known position and heat status."""

    rider_id: str
    latitude: float
    longitude: float
    status: str = "unknown"          # safe | warning | danger | unknown
    temperature_c: float | None = None
    hours_above_threshold: float | None = None
    label: str | None = None
    source: str = "unknown"          # live | mock | unavailable
    observed_date: str | None = None
    updated_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Event:
    """One line in the live alert feed."""

    kind: str                        # evaluation | message | system
    text: str
    status: str = "info"             # safe | warning | danger | unknown | info
    rider_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    temperature_c: float | None = None
    #: The published NOAA/OSHA threshold this reading crossed, if any. Shown
    #: under the feed line so a dispatcher defending a stopped shift has the
    #: citation on screen rather than having to look it up.
    citation: str | None = None
    #: A compact route comparison for the map, from
    #: :meth:`~safety_rider.routing.RouteComparison.to_map_payload`. Only route
    #: events carry it, and it holds the thinned geometry rather than every
    #: OSRM vertex — this rides an SSE frame.
    route: dict[str, Any] | None = None
    at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Hub:
    """Fan-out event bus plus the current rider snapshot."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._riders: dict[str, RiderState] = {}
        self._history: deque[Event] = deque(maxlen=HISTORY_LIMIT)
        #: The most recent route comparison, kept so a dashboard opened after
        #: the comparison ran still paints it. Events replay from history, but
        #: a browser joining mid-demo would otherwise show an empty map for the
        #: one feature the demo is built around.
        self._last_route: dict[str, Any] | None = None

    # ── state ──────────────────────────────────────────────────────────────

    def upsert_rider(self, state: RiderState) -> None:
        self._riders[state.rider_id] = state
        self._save()

    def riders(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._riders.values()]

    def find_rider(self, rider_id: str) -> RiderState | None:
        """The current state for one rider, or None."""
        return self._riders.get(rider_id)

    def history(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._history]

    def last_route(self) -> dict[str, Any] | None:
        """The most recent route comparison, for first paint."""
        return self._last_route

    def clear(self) -> None:
        """Reset between demo runs."""
        self._riders.clear()
        self._history.clear()
        self._last_route = None
        self._save()

    # ── persistence ────────────────────────────────────────────────────────
    #
    # Only the rider registry is persisted, never the event feed. The feed is a
    # display buffer and losing it costs a dispatcher nothing; the registry is
    # the origin a routing request is answered from, and losing THAT told a
    # rider who pinned thirty seconds ago that we had no idea where they were.
    # Railway redeploys often enough during a hackathon for that to be the
    # normal case rather than an edge one.

    def _save(self) -> None:
        """Write the registry to disk. Never raises — this is a side effect.

        Written via a temp file and an atomic rename so a crash mid-write
        cannot leave a truncated file that fails to parse on the next boot.
        """
        try:
            REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"riders": self.riders()})
            with tempfile.NamedTemporaryFile(
                "w", dir=REGISTRY_PATH.parent, prefix=".riders-", suffix=".tmp",
                delete=False, encoding="utf-8",
            ) as handle:
                handle.write(payload)
                temp_name = handle.name
            os.replace(temp_name, REGISTRY_PATH)
        except OSError as exc:
            log.warning("Could not persist the rider registry: %s", exc)

    def load(self) -> int:
        """Restore the registry from disk, dropping anything past the TTL.

        Returns the number of riders restored. Called once at startup; a
        missing, unreadable, or malformed file is not an error — it means this
        is a cold boot, and an empty registry is a correct empty registry.
        """
        try:
            raw = REGISTRY_PATH.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return 0

        try:
            entries = json.loads(raw).get("riders") or []
        except (ValueError, AttributeError):
            log.warning("Rider registry at %s is malformed — starting empty.", REGISTRY_PATH)
            return 0

        cutoff = datetime.now(tz=timezone.utc) - REGISTRY_TTL
        restored = 0
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("rider_id"):
                continue
            try:
                seen = datetime.fromisoformat(entry.get("updated_at", ""))
            except ValueError:
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen < cutoff:
                continue
            try:
                self._riders[entry["rider_id"]] = RiderState(**entry)
            except TypeError:
                # A field was added or removed since this file was written.
                # Skip the row rather than refusing to boot on stale schema.
                continue
            restored += 1

        if restored:
            log.info("Restored %d rider(s) from %s.", restored, REGISTRY_PATH)
        # Expired rows are dropped from disk too, not just from memory.
        if restored != len(entries):
            self._save()
        return restored

    # ── pub/sub ────────────────────────────────────────────────────────────

    def publish(self, event: Event) -> None:
        """Record an event and push it to every live dashboard.

        Synchronous and non-blocking on purpose: it is called from the message
        pipeline, which must never wait on a browser.
        """
        self._history.append(event)
        if event.route:
            self._last_route = event.route
        payload = json.dumps(event.to_dict())
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Subscriber is not draining. Drop it rather than stall here.
                log.warning("Dropping a saturated dashboard subscriber.")
                self._subscribers.discard(queue)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        log.info("Dashboard subscribed (%d live).", len(self._subscribers))
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            log.info("Dashboard unsubscribed (%d live).", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


#: Module-level singleton. Imported by both the pipeline and the dashboard.
hub = Hub()

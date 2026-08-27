"""Per-rider rate limiting for the inbound message pipeline.

Nothing upstream of this bounds how often one number can make the service work.
Meta authenticates the *sender* of a webhook, not the rider inside it, so a
signed payload is proof it came from Meta and says nothing about whether the
rider on the other end is pinning once an hour or once a second. Two costs sit
behind each message:

* **Billed heat requests.** A pin in a cold grid cell costs two FortyGuard
  heatmap requests; a routing request costs up to ``max_route_cells`` × 2. The
  per-(cell, date) cache means a rider standing still is nearly free, but one
  moving across cells — or a script sending scattered coordinates — is not.
  ``max_route_cells`` caps a single route and nothing capped the rate.
* **Outbound WhatsApp messages.** Every inbound message produces at least one
  reply, and Meta meters those against the business number's own throughput.

Two budgets, because the costs differ by an order of magnitude: a general one
for messages, and a tighter one for routing requests.

**Telling the rider they are throttled costs a message too**, so it is said
once per window and then not again. Replying to every throttled message would
turn a limiter into a 1:1 amplifier — precisely the spend it exists to stop.

**Scope: one process**, like the dedup cache in :mod:`~safety_rider.whatsapp.
webhook` and the hub in :mod:`safety_rider.events`. Two workers give a rider
two budgets. All three want Redis at the same time; see the note in
``events.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Riders tracked before the least recently seen are evicted. A bound is needed
#: because the key is a phone number and nothing stops an attacker minting new
#: ones. Eviction only forgets that someone was throttled — it cannot grant
#: more budget than the window already allows, because the window is shorter
#: than any realistic time to cycle through this many distinct numbers.
MAX_TRACKED_RIDERS = 10_000


@dataclass(frozen=True)
class Decision:
    """The outcome of one rate-limit check."""

    #: True when the caller should go ahead and do the work.
    allowed: bool
    #: True only on the FIRST rejection in a window. The caller uses this to
    #: tell the rider once and then stay quiet.
    should_notify: bool = False
    #: Whole seconds until the oldest recorded hit falls out of the window, so
    #: the rider can be told when to come back rather than just "no".
    retry_after_s: int = 0

    def __bool__(self) -> bool:
        return self.allowed


class RateLimiter:
    """A sliding-window counter, keyed by rider.

    A sliding window rather than a token bucket: the budget here is billed API
    requests over a period, which is exactly what a window measures. A bucket
    would also permit a full-size burst the moment it refills, and a burst is
    the shape of the problem — a rider tapping *send* twenty times because the
    first reply was slow.

    ``limit <= 0`` disables the limiter entirely, which is what lets a demo run
    without one.
    """

    def __init__(self, limit: int, window_s: float, *, name: str = "rate") -> None:
        self.limit = limit
        self.window_s = window_s
        self.name = name
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._notified: dict[str, float] = {}
        # The pipeline calls this from the event loop, but the module is
        # importable from a worker thread and the warmer runs off one. A lock
        # is cheaper than reasoning about which callers are which.
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, key: str, *, now: float | None = None) -> Decision:
        """Record a hit for ``key`` and say whether it is within budget.

        Recording and deciding are one operation on purpose: separating them
        invites a caller to check, do the work, and forget to record it.
        """
        if not self.enabled:
            return Decision(allowed=True)

        moment = time.monotonic() if now is None else now
        cutoff = moment - self.window_s

        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
            self._hits.move_to_end(key)

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) < self.limit:
                hits.append(moment)
                self._notified.pop(key, None)
                self._evict_locked()
                return Decision(allowed=True)

            # Over budget. The rejected message is deliberately NOT recorded:
            # counting it would extend the window every time someone retried,
            # so a rider hammering the service could never climb out of it.
            retry_after = max(1, int(hits[0] + self.window_s - moment + 0.999))
            first = self._notified.get(key) is None
            if first:
                self._notified[key] = moment
            self._evict_locked()

        if first:
            log.warning(
                "Rider %s exceeded the %s budget (%d per %.0fs); throttling for %ds.",
                key, self.name, self.limit, self.window_s, retry_after,
            )
        return Decision(allowed=False, should_notify=first, retry_after_s=retry_after)

    def _evict_locked(self) -> None:
        """Drop the least recently seen riders. Caller must hold the lock."""
        while len(self._hits) > MAX_TRACKED_RIDERS:
            stale, _ = self._hits.popitem(last=False)
            self._notified.pop(stale, None)

    def refund(self, key: str) -> None:
        """Give back the most recent hit recorded for ``key``.

        For a message that passed this budget and was then refused by a
        narrower one downstream. Without it, a rider who spams the expensive
        feature burns the general budget on requests that were never served —
        and the general budget is what the plain safety check runs on. Losing
        the ability to ask "is it safe here?" by misusing route comparison is
        the one way a cost control must not fail.
        """
        if not self.enabled:
            return
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()

    def reset(self, key: str | None = None) -> None:
        """Forget one rider's history, or everyone's. For tests and demos."""
        with self._lock:
            if key is None:
                self._hits.clear()
                self._notified.clear()
            else:
                self._hits.pop(key, None)
                self._notified.pop(key, None)

    def remaining(self, key: str, *, now: float | None = None) -> int:
        """Hits left in the current window. Unbounded when disabled."""
        if not self.enabled:
            return self.limit if self.limit > 0 else 2**31
        moment = time.monotonic() if now is None else now
        cutoff = moment - self.window_s
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return self.limit
            live = sum(1 for hit in hits if hit > cutoff)
            return max(0, self.limit - live)


def throttle_message(retry_after_s: int) -> str:
    """What a throttled rider is told. Said once per window, never repeated."""
    minutes = max(1, round(retry_after_s / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return (
        "⏳ You're sending locations faster than I can usefully check them.\n\n"
        f"Give me about {minutes} {unit} and send your location again — I'll "
        "pick straight back up.\n\n"
        "_If this is an emergency, don't wait on me: call emergency services._"
    )


def throttle_route_message(retry_after_s: int) -> str:
    """The routing-specific version. Routing has its own, tighter budget."""
    minutes = max(1, round(retry_after_s / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return (
        "⏳ That's a lot of route comparisons in a short window — each one "
        "prices the heat along several blocks, so they're rationed.\n\n"
        f"Try again in about {minutes} {unit}. You can still send your "
        "location for a safety check in the meantime."
    )

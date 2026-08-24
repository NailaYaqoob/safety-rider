"""Risk analysis engine — turn a temperature into a decision and a protocol.

The bands are the operational ones agreed for this service, in dry-bulb 2 m air
temperature (°C):

===========  ==================  ================================================
Band         Range               Response
===========  ==================  ================================================
Safe         under 35            Ride normally.
Warning      35 up to 40         Hydration protocol.
Danger       40 and above        Automated rest protocol + cooler re-routing.
===========  ==================  ================================================

The spec wrote Warning as "35 °C to 39 °C" and Danger as "40 °C+", which leaves
39.0–40.0 unclaimed. A rider at 39.6 °C must land somewhere, so Warning is
implemented as a half-open interval, ``35.0 <= t < 40.0``. The bands therefore
partition the line with no gap and no overlap. :data:`DANGER_THRESHOLD_C` is
the single place to change if you want 39.0 to be the Danger cutoff instead.

**This is not the same scale as** :mod:`safety_rider.heat_risk`. That module
bands against published NOAA/OSHA heat-index thresholds and is what you cite
when someone asks why a warning fired. This module is the rider-facing
operational protocol: coarser, more decisive, and the one the WhatsApp reply is
built from. They are kept separate rather than reconciled because they answer
different questions — "is this defensible?" versus "what do I do right now?"
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)

# ── Band edges (°C, dry-bulb 2 m air temperature) ──────────────────────────
WARNING_THRESHOLD_C = 35.0   # at/above this: hydration protocol
DANGER_THRESHOLD_C = 40.0    # at/above this: rest protocol + re-routing

#: Sustained exposure that promotes an otherwise-Safe reading to Warning. Four
#: hours above the OSHA high-heat line is a harder day than twenty minutes at a
#: higher peak, and a peak-only reading cannot see the difference.
#: Offered when a reading failed but might succeed later. Named so the
#: renderer can drop it for permanent failures without matching prose.
_RETRY_ACTION = "Send your location again in a few minutes and I'll retry."

SUSTAINED_HOURS = 4.0
#: Above this, a Warning day gets an extra "go early or go late" line. It does
#: NOT promote to Danger — the 40 °C cutoff above is the spec and stays intact.
LONG_EXPOSURE_HOURS = 8.0


class SafetyStatus(str, Enum):
    """Ordered bands. String-valued so they serialise straight to JSON."""

    SAFE = "safe"
    WARNING = "warning"
    DANGER = "danger"
    #: No usable temperature. Not a band — the absence of one.
    UNKNOWN = "unknown"


#: WhatsApp renders emoji inline, and they stay legible on a phone in direct
#: sun in a way a colour never would.
_BADGE = {
    SafetyStatus.SAFE: "🟢",
    SafetyStatus.WARNING: "🟡",
    SafetyStatus.DANGER: "🔴",
    SafetyStatus.UNKNOWN: "❔",
}

_HEADLINE = {
    SafetyStatus.SAFE: "Clear to ride",
    SafetyStatus.WARNING: "Hot — hydrate and pace yourself",
    SafetyStatus.DANGER: "Dangerous heat — rest protocol active",
    SafetyStatus.UNKNOWN: "Couldn't read the temperature here",
}


@dataclass(frozen=True)
class RiderSafetyStatus:
    """A decision, the actions that follow from it, and why it fired."""

    status: SafetyStatus
    #: The temperature this decision was made on, or None when unknown.
    temperature_c: float | None
    headline: str
    #: What the rider should actually do, in the order they should do it.
    actions: list[str] = field(default_factory=list)
    #: True when the automated rest protocol should fire (Danger band).
    rest_protocol: bool = False
    #: True when a cooler alternative route should be offered (Danger band).
    reroute: bool = False
    #: One line naming what escalated or held the band, for logs and audit.
    reason: str = ""
    hours_above_threshold: float | None = None

    @property
    def fahrenheit(self) -> float | None:
        if self.temperature_c is None:
            return None
        return self.temperature_c * 9 / 5 + 32

    def to_whatsapp_text(self, reading: object | None = None) -> str:
        """Render the tailored notification body (WhatsApp markdown: ``*bold*``).

        ``reading`` is an optional :class:`~safety_rider.temperature_service.
        TemperatureReading`; when given, its provenance line is appended so the
        rider can see whether the number was measured or simulated, at what
        resolution, and — critically — **for which day**.
        """
        lines = [f"{_BADGE[self.status]} *{self.headline}*", ""]

        if self.temperature_c is None:
            reason = getattr(reading, "error", None)
            lines.append(
                # The reason may already end in an abbreviation's full stop.
                f"I can't check that spot — {reason.rstrip('.')}."
                if reason
                else "I couldn't get a temperature for that spot."
            )
            lines.append(
                "So I can't tell you whether it's safe. Treat the heat as "
                "unknown and carry water."
            )
        else:
            # With a nowcast the headline number is the current hour, not the
            # day's peak. Calling it a peak would misdescribe it by several
            # degrees in either direction, so the wording follows the source.
            now_c = getattr(reading, "now_celsius", None)
            lines.append(
                (f"Right now where you are: *{self.temperature_c:.1f} °C* "
                 f"({self.fahrenheit:.0f} °F).")
                if now_c is not None else
                (f"Peak air temperature where you are: *{self.temperature_c:.1f} °C* "
                 f"({self.fahrenheit:.0f} °F).")
            )
            if self.hours_above_threshold is not None and self.hours_above_threshold >= 1:
                # Duration comes from a complete day, which is not the day the
                # nowcast describes. Name it, or the two blur into one claim.
                when = getattr(reading, "observed_date", None)
                dated = f" (measured {when})" if when and now_c is not None else ""
                lines.append(
                    f"That spot spends about *{self.hours_above_threshold:.0f} hours* "
                    f"a day above the high-heat line{dated}."
                )

        actions = self.actions
        if getattr(reading, "permanent", False):
            # Nothing about this location will change on a retry.
            actions = [a for a in actions if a != _RETRY_ACTION]
        if actions:
            lines.append("")
            lines.extend(f"• {action}" for action in actions)

        # Suppressed when there is no reading: the body has already given the
        # reason, and repeating it as provenance reads like a second failure.
        provenance = getattr(reading, "describe", None)
        if callable(provenance) and self.temperature_c is not None:
            lines.append("")
            lines.append(f"_{provenance()}_")

        return "\n".join(lines)


def evaluate_rider_safety_status(
    current_temp: float | None,
    *,
    hours_above_threshold: float | None = None,
) -> RiderSafetyStatus:
    """Categorise ``current_temp`` (°C) into a band and the protocol it triggers.

    ``current_temp`` accepts anything float-like — a bare number or a
    :class:`~safety_rider.temperature_service.TemperatureReading`, which defines
    ``__float__``. ``None`` (or NaN) yields :attr:`SafetyStatus.UNKNOWN` rather
    than an exception: this runs inside a webhook background task, and a
    temperature that failed to arrive is an expected outcome, not a bug.

    ``hours_above_threshold`` is optional and additive. When it shows sustained
    exposure it can promote Safe to Warning; it never promotes to Danger, which
    stays anchored to the 40 °C cutoff.
    """
    temp = _as_float(current_temp)

    if temp is None:
        return RiderSafetyStatus(
            status=SafetyStatus.UNKNOWN,
            temperature_c=None,
            headline=_HEADLINE[SafetyStatus.UNKNOWN],
            actions=[
                "Carry water and assume it is hotter than it looks.",
                _RETRY_ACTION,
            ],
            reason="no usable temperature value",
            hours_above_threshold=hours_above_threshold,
        )

    # ── Band selection ────────────────────────────────────────────────────
    if temp >= DANGER_THRESHOLD_C:
        status, reason = SafetyStatus.DANGER, f"{temp:.1f} °C ≥ {DANGER_THRESHOLD_C} °C"
    elif temp >= WARNING_THRESHOLD_C:
        status, reason = SafetyStatus.WARNING, f"{temp:.1f} °C in [{WARNING_THRESHOLD_C}, {DANGER_THRESHOLD_C})"
    else:
        status, reason = SafetyStatus.SAFE, f"{temp:.1f} °C < {WARNING_THRESHOLD_C} °C"

    # ── Duration override ─────────────────────────────────────────────────
    sustained = (
        hours_above_threshold is not None
        and hours_above_threshold >= SUSTAINED_HOURS
    )
    if status is SafetyStatus.SAFE and sustained:
        status = SafetyStatus.WARNING
        reason += f", promoted on {hours_above_threshold:.0f} h sustained exposure"

    actions = _actions_for(status, temp, hours_above_threshold)

    result = RiderSafetyStatus(
        status=status,
        temperature_c=round(temp, 1),
        headline=_HEADLINE[status],
        actions=actions,
        rest_protocol=status is SafetyStatus.DANGER,
        reroute=status is SafetyStatus.DANGER,
        reason=reason,
        hours_above_threshold=hours_above_threshold,
    )
    log.info("Safety evaluation: %s (%s)", result.status.value, result.reason)
    return result


#: The camelCase name from the integration spec; see the note in
#: :mod:`safety_rider.temperature_service` for why the implementation is
#: snake_case.
evaluateRiderSafetyStatus = evaluate_rider_safety_status  # noqa: N816


# ─────────────────────────────────────────────────────────────── internals


def _as_float(value: object) -> float | None:
    """Coerce to a finite float, or None. Never raises."""
    if value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _actions_for(
    status: SafetyStatus,
    temp: float,
    hours_above_threshold: float | None,
) -> list[str]:
    """The protocol for a band. Empty for Safe — don't nag when it's fine."""
    if status is SafetyStatus.SAFE:
        return []

    if status is SafetyStatus.WARNING:
        actions = [
            "Drink now, and every 15–20 minutes — before you feel thirsty.",
            "Take a shaded break every 30 minutes.",
            "Loose, light clothing; keep your head covered.",
        ]
        if hours_above_threshold is not None and hours_above_threshold >= LONG_EXPOSURE_HOURS:
            actions.append(
                f"This spot stays hot for ~{hours_above_threshold:.0f} hours — "
                "ride before 10am or after 6pm if the trip can move."
            )
        return actions

    # DANGER — the automated rest protocol.
    return [
        "*Stop riding now* and get into shade or air conditioning.",
        "Rest 15 minutes minimum before moving again. Drink throughout.",
        "Watch for heat stroke: confusion, dry skin, no sweating, nausea. "
        "If any of those appear, call emergency services.",
        "Tell your dispatcher or a friend where you are.",
        cooler_route_hint(temp),
    ]


def cooler_route_hint(temp: float) -> str:
    """Tell the rider how to ask for a cooler route.

    The comparison itself lives in :mod:`safety_rider.routing`; this is only the
    prompt. It stays here so the Danger protocol reads as one list of actions,
    and so ``rider_status`` keeps no dependency on the routing module — the
    bands must be evaluable without a network.
    """
    return (
        "Reply with your destination as `to <lat>,<lon>` and I'll compare "
        "routes for heat exposure."
    )

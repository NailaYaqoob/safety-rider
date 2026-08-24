"""Meta WhatsApp Cloud API webhook — GET verification and POST message intake.

Mounted by :mod:`safety_rider.app` at ``/webhook/whatsapp``. That full path is
what you paste into the Meta App Dashboard as the Callback URL.

Both handlers are deliberately defensive about status codes, because Meta treats
them as control signals:

* GET  — return the raw challenge with 200, or 403. Anything else and Meta
  refuses to subscribe the webhook.
* POST — return 200 fast and almost unconditionally. A non-2xx (or a slow
  response) makes Meta retry the same payload with backoff for hours, so a bug
  in message handling would turn into a retry storm. Real work happens in a
  background task after the ack.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections import OrderedDict
from typing import Any, Awaitable

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response, status

from ..config import settings
from ..events import Event, RiderState, hub, mask_number
from ..models import RiderLocation
from ..rider_status import RiderSafetyStatus, evaluate_rider_safety_status
from ..routing import compare_routes
from ..temperature_service import get_hyperlocal_temperature
from . import graph_client
from .models import InboundMessage
from .parser import coordinates_from_text, destination_from_text, iter_inbound_messages

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp"])

#: Meta redelivers on any non-2xx, and at-least-once delivery means the same
#: wamid can legitimately arrive twice. Keep a bounded set of seen ids so a
#: retry does not double-reply.
#:
#: In-process only — fine for one worker, wrong the moment you scale out.
#: Replace with Redis (SETNX + TTL) before running more than one instance.
_SEEN_MESSAGE_IDS: OrderedDict[str, None] = OrderedDict()
_SEEN_LIMIT = 2048


def _already_processed(message_id: str) -> bool:
    """True if this wamid was handled before; records it otherwise."""
    if message_id in _SEEN_MESSAGE_IDS:
        return True
    _SEEN_MESSAGE_IDS[message_id] = None
    while len(_SEEN_MESSAGE_IDS) > _SEEN_LIMIT:
        _SEEN_MESSAGE_IDS.popitem(last=False)
    return False


# ─────────────────────────────────────────────────────────── 1. verification


@router.get("", include_in_schema=False)
@router.get("/")
async def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Meta's one-time subscription handshake.

    When you save a Callback URL in the App Dashboard, Meta issues a GET with
    ``hub.mode=subscribe``, the verify token you typed into the dashboard, and a
    random ``hub.challenge``. Echo the challenge back **as a bare 200 body** —
    not wrapped in JSON, or the handshake fails — and only when the token
    matches.

    The dotted query names cannot be Python identifiers, hence the aliases.
    """
    if not settings.verify_token:
        log.error("WHATSAPP_VERIFY_TOKEN is not set — cannot verify webhook.")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    token_ok = hub_verify_token is not None and hmac.compare_digest(
        hub_verify_token, settings.verify_token
    )

    if hub_mode == "subscribe" and token_ok and hub_challenge is not None:
        log.info("Webhook verification succeeded.")
        # media_type matters: Meta wants the challenge verbatim, not quoted.
        return Response(content=hub_challenge, media_type="text/plain")

    log.warning(
        "Webhook verification rejected (mode=%r, token_ok=%s)", hub_mode, token_ok
    )
    return Response(status_code=status.HTTP_403_FORBIDDEN)


# ──────────────────────────────────────────────────────── 2. message intake


def _signature_is_valid(raw_body: bytes, header_value: str | None) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` over the **raw** request body.

    Without this, anyone who discovers the callback URL can POST fabricated
    rider messages and make the service send WhatsApp messages on your behalf.

    The HMAC is computed over the exact bytes Meta sent — re-serialising the
    parsed JSON produces different bytes and will never match.
    """
    if not settings.app_secret:
        log.error("WHATSAPP_APP_SECRET is not set — rejecting unverifiable payload.")
        return False
    if not header_value or not header_value.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))


@router.post("", include_in_schema=False)
@router.post("/")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> Response:
    """Receive inbound messages, ack immediately, handle them in the background."""
    raw_body = await request.body()

    if not _signature_is_valid(raw_body, x_hub_signature_256):
        # The one case worth refusing: an unsigned or wrongly-signed payload is
        # not from Meta, so there is no retry storm to worry about.
        log.warning("Rejected webhook POST with an invalid signature.")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = await request.json()
    except ValueError:
        log.warning("Webhook POST carried a body that is not valid JSON.")
        return Response(status_code=status.HTTP_200_OK)

    if settings.debug_payloads:
        # Contains rider phone numbers and precise locations — off by default.
        log.debug("Inbound payload: %s", payload)

    for message in iter_inbound_messages(payload):
        if _already_processed(message.message_id):
            log.info("Skipping duplicate delivery of %s", message.message_id)
            continue
        # Queued, not awaited: the response goes out first and the evaluation
        # runs after, which is what keeps us inside Meta's timeout.
        background_tasks.add_task(handle_message, message)

    # Always 200 once the payload is authenticated, even if nothing in it was a
    # message we act on — status callbacks land here constantly.
    return Response(status_code=status.HTTP_200_OK)


# ───────────────────────────────────────────────────────── 3. message handling

async def handle_message(message: InboundMessage) -> None:
    """The unified controller: locate the rider, measure, decide, reply.

    Runs *after* the webhook has already returned 200, so it may take as long
    as the FortyGuard polling loop needs. Nothing in here is allowed to
    propagate: there is no response left to fail, and an exception escaping a
    background task would be silent to the rider and invisible in the logs
    except as a traceback with no context.

    Flow::

        location pin (or typed "lat, lon")
          -> get_hyperlocal_temperature()      # blocking, worker thread
          -> evaluate_rider_safety_status()    # pure, instant
          -> Graph API text reply              # tailored to the band
    """
    try:
        await _safe_graph_call(
            graph_client.mark_as_read(message.message_id), "read receipt"
        )

        # A destination is a routing request, not a position update, so it is
        # checked first — otherwise "to 37.4,-122.1" would be read as a pin.
        destination = destination_from_text(message.text)
        if destination is not None:
            await _compare_and_reply(message, destination)
            return

        location = message.location or coordinates_from_text(message.text)
        if location is None:
            # Nothing to evaluate. Ask for a pin — it is a two-tap action in
            # the WhatsApp attachment menu.
            await _safe_graph_call(
                graph_client.send_text(message.from_number, _location_prompt(message)),
                "location prompt",
            )
            return

        await _evaluate_and_reply(message, location)

    except Exception:  # noqa: BLE001 — background task: log, never propagate.
        log.exception("Failed to handle message %s", message.message_id)


async def _evaluate_and_reply(message: InboundMessage, location: RiderLocation) -> None:
    """Measure the rider's location, decide, and send the tailored reply."""
    # The FortyGuard endpoints are submit-then-poll and can take tens of
    # seconds on a cache miss. Say something first — silence reads as a broken
    # bot — then run the blocking lookup in a worker thread so the event loop
    # stays free for other riders.
    await _safe_graph_call(
        graph_client.send_text(
            message.from_number, "🌡️ Checking the heat where you are — one moment…"
        ),
        "acknowledgement",
    )

    # Never raises: failure comes back as a reading with ok=False.
    reading = await asyncio.to_thread(
        get_hyperlocal_temperature, location.latitude, location.longitude
    )

    # decision_celsius is the current hour when the nowcast landed, the daily
    # peak otherwise — a rider on the street is asking what it is now.
    status = evaluate_rider_safety_status(
        reading.decision_celsius if reading.ok else None,
        hours_above_threshold=reading.hours_above_threshold,
    )

    log.info(
        "Rider %s at %.5f,%.5f → %.1f °C (%s) → %s",
        message.from_number,
        location.latitude,
        location.longitude,
        reading.decision_celsius if reading.ok else float("nan"),
        reading.source,
        status.status.value,
    )

    publish_evaluation(
        from_number=message.from_number,
        location=location,
        status=status,
        reading=reading,
        label=message.profile_name,
    )

    await _safe_graph_call(
        graph_client.send_text(message.from_number, status.to_whatsapp_text(reading)),
        "safety notification",
    )

    if status.rest_protocol:
        await _trigger_rest_protocol(message, location, status)


async def _compare_and_reply(message: InboundMessage, destination: RiderLocation) -> None:
    """Answer a routing request by comparing candidate routes for heat.

    The origin is the rider's last known position, which the hub is already
    tracking. Asking them to re-send it would be the kind of friction that
    stops a courier using the thing at all.
    """
    origin = _last_known_location(message.from_number)
    if origin is None:
        await _safe_graph_call(
            graph_client.send_text(
                message.from_number,
                "I don't know where you are yet — share your location first, "
                "then send your destination as `to <lat>,<lon>`.",
            ),
            "route origin prompt",
        )
        return

    await _safe_graph_call(
        graph_client.send_text(
            message.from_number, "🧭 Comparing routes for heat exposure — one moment…"
        ),
        "route acknowledgement",
    )

    comparison = await asyncio.to_thread(compare_routes, origin, destination)

    if comparison is None:
        await _safe_graph_call(
            graph_client.send_text(
                message.from_number,
                "I couldn't find a route between those two points. Check the "
                "coordinates and try again.",
            ),
            "route failure",
        )
        return

    log.info(
        "Route request %s -> %.5f,%.5f: %s",
        message.from_number, destination.latitude, destination.longitude,
        comparison.reason,
    )

    hub.publish(Event(
        kind="route",
        text=(
            f"Rider {_display_id(message.from_number)} requested a route. "
            + (f"Cooler option found — {comparison.reason}."
               if comparison.worth_detour else
               f"Direct route is already coolest ({comparison.reason}).")
        ),
        status="warning" if comparison.worth_detour else "info",
        rider_id=_display_id(message.from_number),
    ))

    await _safe_graph_call(
        graph_client.send_text(message.from_number, comparison.to_whatsapp_text()),
        "route comparison",
    )


def _display_id(from_number: str) -> str:
    return from_number if settings.dashboard_unmask else mask_number(from_number)


def _last_known_location(from_number: str) -> RiderLocation | None:
    """The rider's most recent position, from the dashboard hub's registry."""
    rider_id = _display_id(from_number)
    for rider in hub.riders():
        if rider["rider_id"] == rider_id:
            return RiderLocation(rider["latitude"], rider["longitude"])
    return None


async def _trigger_rest_protocol(
    message: InboundMessage,
    location: RiderLocation,
    status: RiderSafetyStatus,
) -> None:
    """Danger-band side effects, separated from the reply that announced them.

    The rider has already been told to stop; this is everything that happens
    *around* that — currently a structured log line at WARNING so a Danger
    event is greppable in ops. The dispatcher notification and the cooler
    re-route both hang off this function, which is why it exists as a seam
    rather than being inlined above.
    """
    log.warning(
        "REST PROTOCOL: rider=%s lat=%.5f lon=%.5f temp=%.1fC hours_above=%s",
        message.from_number,
        location.latitude,
        location.longitude,
        status.temperature_c if status.temperature_c is not None else float("nan"),
        status.hours_above_threshold,
    )


async def _safe_graph_call(coro: Awaitable[Any], description: str) -> Any | None:
    """Await an outbound Graph call, logging rather than raising on failure.

    One failed send must not abort the rest of the flow. The common causes are
    both routine and unrecoverable at this point: an expired access token
    (Graph 401), or the rider sitting outside the 24-hour customer service
    window, which needs a template rather than free-form text.
    """
    try:
        return await coro
    except (graph_client.WhatsAppSendError, httpx.HTTPError) as exc:
        log.error("Graph call failed (%s): %s", description, exc)
        return None


def publish_evaluation(
    *,
    from_number: str,
    location: RiderLocation,
    status: RiderSafetyStatus,
    reading: Any,
    label: str | None = None,
) -> None:
    """Push one evaluation onto the dashboard: move the pin, write the feed line.

    Shared by the live webhook path and the demo simulator so the dashboard
    cannot drift from what the pipeline actually decided — a simulated spike
    renders through exactly the same code as a real rider.
    """
    rider_id = from_number if settings.dashboard_unmask else mask_number(from_number)

    hub.upsert_rider(RiderState(
        rider_id=rider_id,
        latitude=location.latitude,
        longitude=location.longitude,
        status=status.status.value,
        temperature_c=status.temperature_c,
        hours_above_threshold=status.hours_above_threshold,
        label=label,
        source=getattr(reading, "source", "unknown"),
        observed_date=getattr(reading, "observed_date", None),
    ))

    where = location.name or f"{location.latitude:.4f}, {location.longitude:.4f}"
    if status.temperature_c is None:
        text = f"Rider {rider_id}: no temperature available at {where}."
    elif status.rest_protocol:
        text = (
            f"Rider {rider_id} entered a {status.temperature_c:.1f} °C zone at "
            f"{where}. Rest protocol triggered — WhatsApp alert sent."
        )
    else:
        text = (
            f"Rider {rider_id} at {where}: {status.temperature_c:.1f} °C — "
            f"{status.status.value}. WhatsApp update sent."
        )

    hub.publish(Event(
        kind="evaluation",
        text=text,
        status=status.status.value,
        rider_id=rider_id,
        latitude=location.latitude,
        longitude=location.longitude,
        temperature_c=status.temperature_c,
    ))


def _location_prompt(message: InboundMessage) -> str:
    """Reply used when a rider writes in without sharing a location."""
    greeting = f"Hi {message.profile_name}! " if message.profile_name else "Hi! "
    return (
        f"{greeting}I check how hot it is exactly where you are, and tell you "
        "whether it is safe to ride.\n\n"
        "_Tap 📎 → Location → Send your current location._\n\n"
        "You can also just type coordinates, e.g. `37.3318, -121.8899`."
    )

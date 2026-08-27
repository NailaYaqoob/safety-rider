"""What a Danger verdict actually does, beyond telling the rider.

The rest protocol used to be a log line and `reroute` was a flag nobody read.
This covers the two things that now hang off it: escalation to a dispatcher,
and an automatic cooler route to wherever the rider last said they were going.

Run:  venv/bin/python tests/test_escalation.py
"""
import asyncio, os, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Pinned BEFORE the app import. See tests/README.md.
os.environ["SAFETY_RIDER_REGISTRY_PATH"] = str(
    pathlib.Path(tempfile.mkdtemp(prefix="safety-rider-test-")) / "riders.json")
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
os.environ["SAFETY_RIDER_MOCK_TEMP_C"] = "41.5"          # always Danger
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_APP_SECRET"] = "test-app-secret"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = ""
os.environ["SAFETY_RIDER_DISPATCHER_NUMBER"] = "14155559999"

from safety_rider.events import DESTINATION_TTL, RiderState, hub
from safety_rider.models import RiderLocation
from safety_rider.rider_status import evaluate_rider_safety_status
from safety_rider.routing import RouteCandidate, RouteComparison
from safety_rider.whatsapp import graph_client, webhook
from safety_rider.whatsapp.models import InboundMessage

DISPATCHER = "14155559999"
RIDER = "14155550123"
MASKED = "+14155****123"

ok = True


def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


sent: list[tuple[str, str]] = []


async def _capture(to_number, body, **kwargs):
    sent.append((to_number, body))
    return {"messages": [{"id": "wamid.captured"}]}


async def _noop(*args, **kwargs):
    return None


graph_client.send_text = _capture
graph_client.mark_as_read = _noop


def message(index, *, text=None, location=True, number=RIDER):
    return InboundMessage(
        message_id=f"wamid.{number}.{index}",
        from_number=number,
        message_type="location" if location else "text",
        timestamp=None,
        text=text,
        location=RiderLocation(33.4484, -112.0740) if location else None,
        profile_name="Asha",
    )


def cooler_comparison():
    fast = RouteCandidate("Direct", 8000, 1800,
                          coordinates=[[-112.07, 33.44], [-112.07, 33.45]])
    cool = RouteCandidate("Detour 1", 11000, 2400,
                          coordinates=[[-112.09, 33.44], [-112.09, 33.45]])
    fast.peak_c, fast.degree_hours, fast.coverage = 44.2, 6.5, 1.0
    cool.peak_c, cool.degree_hours, cool.coverage = 30.2, 0.5, 1.0
    return RouteComparison(fast, cool, [fast, cool], True, "saves 6.00 C·h")


def no_better_route():
    fast = RouteCandidate("Direct", 8000, 1800,
                          coordinates=[[-112.07, 33.44], [-112.07, 33.45]])
    fast.peak_c, fast.degree_hours, fast.coverage = 40.0, 5.0, 1.0
    return RouteComparison(fast, fast, [fast], False, "not worth it")


def reset(compare=None):
    webhook._message_limiter.reset()
    webhook._route_limiter.reset()
    hub.clear()
    sent.clear()
    webhook.compare_routes = (lambda origin, dest: compare) if compare else None


def to_rider():
    return [b for to, b in sent if to == RIDER]


def to_dispatcher():
    return [b for to, b in sent if to == DISPATCHER]


run = asyncio.run

print("\n[1] The Danger band still sets both flags")
danger = evaluate_rider_safety_status(41.5)
check("rest_protocol is set", danger.rest_protocol is True)
check("reroute is set", danger.reroute is True)
safe = evaluate_rider_safety_status(20.0)
check("neither fires below the band",
      safe.rest_protocol is False and safe.reroute is False)

print("\n[2] A Danger verdict reaches the dispatcher")
# A fleet buys this so somebody with authority hears about a stopped rider
# when it happens, not when the drop runs late.
reset()
run(webhook.handle_message(message(1)))
escalations = to_dispatcher()
check(f"exactly one message goes to the dispatcher (got {len(escalations)})",
      len(escalations) == 1)
body = escalations[0] if escalations else ""
check("it names the rest protocol", "Rest protocol" in body)
check("the rider's number is masked, as everywhere else", MASKED in body)
check("their profile name is included so dispatch knows who", "Asha" in body)
check("it carries the coordinates", "33.44840" in body)
check("and the temperature", "41.5" in body)
check("and the published threshold", "NOAA Danger" in body)
check("with a map link a dispatcher can actually tap", "maps.google.com" in body)
check("the rider still got their own warning first",
      any("rest protocol active" in b for b in to_rider()))

events = [e for e in hub.history() if e["kind"] == "system"]
check("the console records the escalation",
      any("Dispatcher notified" in e["text"] for e in events))

print("\n[3] A Safe rider escalates nothing")
os.environ["SAFETY_RIDER_MOCK_TEMP_C"] = "20.0"
import importlib

import safety_rider.config as config_module
import safety_rider.temperature_service as temperature_service
real_settings = temperature_service.settings


class MildSettings:
    def __getattr__(self, name):
        if name == "mock_temp_c":
            return 20.0
        return getattr(real_settings, name)


temperature_service.settings = MildSettings()
try:
    reset()
    run(webhook.handle_message(message(2)))
    check(f"nothing is sent to the dispatcher (got {len(to_dispatcher())})",
          to_dispatcher() == [])
    check("the rider is told they are clear to ride",
          any("Clear to ride" in b for b in to_rider()))
finally:
    temperature_service.settings = real_settings
    os.environ["SAFETY_RIDER_MOCK_TEMP_C"] = "41.5"

print("\n[4] A destination is remembered when a rider asks for a route")
reset(compare=cooler_comparison())
run(webhook.handle_message(message(3)))              # establishes an origin
webhook._message_limiter.reset()
run(webhook.handle_message(message(4, text="to 33.55,-112.15", location=False)))
rider = hub.find_rider(MASKED)
check("the rider record exists", rider is not None)
check(f"and holds the destination (got {rider.fresh_destination() if rider else None})",
      rider is not None and rider.fresh_destination() == (33.55, -112.15))

print("\n[5] A later evaluation does not erase it")
# upsert_rider replaces the record, and an evaluation knows where the rider IS,
# not where they are going — so the destination has to be carried across.
webhook._message_limiter.reset()
run(webhook.handle_message(message(5)))
rider = hub.find_rider(MASKED)
check("the destination survives a fresh evaluation",
      rider is not None and rider.fresh_destination() == (33.55, -112.15))

print("\n[6] Danger offers a cooler route without being asked")
# Making someone who has just been told to stop riding type out coordinates is
# the moment they close WhatsApp.
reset(compare=cooler_comparison())
run(webhook.handle_message(message(6)))                       # origin
webhook._message_limiter.reset()
run(webhook.handle_message(message(7, text="to 33.55,-112.15", location=False)))
webhook._message_limiter.reset()
webhook._route_limiter.reset()
sent.clear()
run(webhook.handle_message(message(8)))                       # Danger again

offers = [b for b in to_rider() if "I know where you were heading" in b]
check(f"a cooler route is offered unprompted (got {len(offers)})", len(offers) == 1)
check("and it contains the actual comparison",
      offers and "Cooler route found" in offers[0])
check("the dispatcher was told as well", len(to_dispatcher()) == 1)

route_events = [e for e in hub.history() if e["kind"] == "route" and e.get("route")]
check("the automatic route is drawn on the console too", len(route_events) >= 1)
check("and is labelled as automatic",
      any("Automatic cooler route" in e["text"] for e in route_events))

print("\n[7] Nothing is offered when there is nothing worth offering")
# A Danger message is already long. Appending "I could not find a better route"
# would bury the instruction that matters under one that does not.
reset(compare=no_better_route())
run(webhook.handle_message(message(9)))
webhook._message_limiter.reset()
run(webhook.handle_message(message(10, text="to 33.55,-112.15", location=False)))
webhook._message_limiter.reset()
webhook._route_limiter.reset()
sent.clear()
run(webhook.handle_message(message(11)))
check("no route offer when the direct way is already coolest",
      not any("I know where you were heading" in b for b in to_rider()))

print("\n[8] And nothing when we do not know where they are going")
reset(compare=cooler_comparison())
run(webhook.handle_message(message(12)))
check("a rider who never asked for a route gets no unprompted offer",
      not any("I know where you were heading" in b for b in to_rider()))

print("\n[9] A stale destination is not routed to")
# Routing someone to where they were going two shifts ago is confidently wrong.
reset(compare=cooler_comparison())
run(webhook.handle_message(message(13)))
stale = hub.find_rider(MASKED)
from datetime import datetime, timedelta, timezone
stale.destination_lat, stale.destination_lon = 33.55, -112.15
stale.destination_at = (datetime.now(timezone.utc) - DESTINATION_TTL
                        - timedelta(minutes=1)).isoformat()
check("fresh_destination() refuses it", stale.fresh_destination() is None)
webhook._message_limiter.reset()
sent.clear()
run(webhook.handle_message(message(14)))
check("so no automatic route is sent",
      not any("I know where you were heading" in b for b in to_rider()))

print("\n[10] The automatic route respects the route budget")
# A rider repeatedly re-entering Danger must not be able to spend billed cells
# without limit just by standing still.
reset(compare=cooler_comparison())
run(webhook.handle_message(message(15)))
webhook._message_limiter.reset()
run(webhook.handle_message(message(16, text="to 33.55,-112.15", location=False)))

offers_seen = 0
for i in range(6):
    webhook._message_limiter.reset()
    sent.clear()
    run(webhook.handle_message(message(20 + i)))
    offers_seen += sum(1 for b in to_rider() if "I know where you were heading" in b)
check(f"automatic offers stop once the route budget is spent (got {offers_seen})",
      0 < offers_seen < 6)

print("\n[11] Escalation is off when no dispatcher is configured")
import safety_rider.whatsapp.webhook as webhook_module
real = webhook_module.settings


class NoDispatcher:
    def __getattr__(self, name):
        if name == "dispatcher_number":
            return None
        return getattr(real, name)


webhook_module.settings = NoDispatcher()
try:
    reset(compare=cooler_comparison())
    run(webhook.handle_message(message(30)))
    check("nothing is sent to a dispatcher that does not exist",
          to_dispatcher() == [])
    check("but the rider is still warned",
          any("rest protocol active" in b for b in to_rider()))
finally:
    webhook_module.settings = real

reset()
print("\n" + ("ALL ESCALATION CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

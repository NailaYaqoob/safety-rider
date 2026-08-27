"""Per-rider rate limiting — the window itself, and the pipeline that uses it.

Run:  venv/bin/python tests/test_rate_limit.py
"""
import asyncio, os, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Offline and non-sending, pinned BEFORE the app import. See tests/README.md.
os.environ["SAFETY_RIDER_REGISTRY_PATH"] = str(
    pathlib.Path(tempfile.mkdtemp(prefix="safety-rider-test-")) / "riders.json")
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_APP_SECRET"] = "test-app-secret"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = ""
# Small budgets so the pipeline tests do not need to send a dozen messages.
os.environ["SAFETY_RIDER_RATE_LIMIT"] = "3"
os.environ["SAFETY_RIDER_RATE_WINDOW_S"] = "300"
os.environ["SAFETY_RIDER_ROUTE_RATE_LIMIT"] = "1"
os.environ["SAFETY_RIDER_ROUTE_RATE_WINDOW_S"] = "900"

from safety_rider.events import hub
from safety_rider.models import RiderLocation
from safety_rider.rate_limit import (MAX_TRACKED_RIDERS, RateLimiter,
                                     throttle_message, throttle_route_message)
from safety_rider.whatsapp import graph_client, webhook
from safety_rider.whatsapp.models import InboundMessage

ok = True


def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


print("\n[1] The window counts, and closes")
lim = RateLimiter(3, 60.0, name="test")
T = 1000.0
verdicts = [lim.check("a", now=T + i) for i in range(5)]
check("the first three are allowed", all(v.allowed for v in verdicts[:3]))
check("the fourth is not", verdicts[3].allowed is False)
check("nor the fifth", verdicts[4].allowed is False)
check("a Decision is truthy when it allows", bool(verdicts[0]) and not bool(verdicts[3]))
check("the rider is told when to come back",
      0 < verdicts[3].retry_after_s <= 60)
check("and is allowed again once the window has passed",
      lim.check("a", now=T + 61).allowed)

print("\n[2] Retrying does not extend the throttle")
# Counting a rejected message would push the window forward every time someone
# retried, so a rider hammering the service could never climb back out of it.
lim2 = RateLimiter(1, 60.0)
lim2.check("b", now=T)
first = lim2.check("b", now=T + 10)
later = lim2.check("b", now=T + 30)
check(f"retry_after shrinks as the window drains ({first.retry_after_s} -> {later.retry_after_s})",
      later.retry_after_s < first.retry_after_s)
check("and the rider does get back in on schedule", lim2.check("b", now=T + 61).allowed)

print("\n[3] The rider is told once, not on every message")
# Replying to each throttled message would make the limiter a 1:1 amplifier —
# exactly the spend it exists to prevent.
lim3 = RateLimiter(1, 10.0)
lim3.check("c", now=T)
check("first rejection notifies", lim3.check("c", now=T + 1).should_notify is True)
check("the second does not", lim3.check("c", now=T + 2).should_notify is False)
check("nor the third", lim3.check("c", now=T + 3).should_notify is False)
check("after recovery the rider is allowed through", lim3.check("c", now=T + 11).allowed)
check("and a fresh breach notifies again",
      lim3.check("c", now=T + 12).should_notify is True)

print("\n[4] Riders have separate budgets")
lim4 = RateLimiter(1, 60.0)
lim4.check("rider-1", now=T)
check("rider-1 is now over budget", lim4.check("rider-1", now=T).allowed is False)
check("rider-2 is not affected", lim4.check("rider-2", now=T).allowed is True)

print("\n[5] The limiter can be switched off")
off = RateLimiter(0, 60.0)
check("limit 0 allows everything", all(off.check("d").allowed for _ in range(50)))
check("and reports itself disabled", off.enabled is False)
check("a negative limit is off too, not inverted", RateLimiter(-5, 60.0).enabled is False)

print("\n[6] Memory is bounded")
# The key is a phone number and nothing stops an attacker minting new ones.
lim6 = RateLimiter(1, 3600.0)
for i in range(MAX_TRACKED_RIDERS + 250):
    lim6.check(f"rider-{i}", now=T)
check(f"tracked riders stay capped at {MAX_TRACKED_RIDERS} "
      f"(got {len(lim6._hits)})", len(lim6._hits) <= MAX_TRACKED_RIDERS)
check("the most recent rider is still tracked",
      f"rider-{MAX_TRACKED_RIDERS + 249}" in lim6._hits)
check("the oldest was evicted", "rider-0" not in lim6._hits)
check("remaining() reports the budget for an unseen rider",
      lim6.remaining("never-seen", now=T) == 1)

print("\n[7] The messages say something useful")
body = throttle_message(120)
check("the throttle notice tells the rider when to retry", "2 minutes" in body)
check("it does not leave an emergency without a route",
      "emergency services" in body)
check("a sub-minute wait still reads sensibly", "1 minute" in throttle_message(20))
route_body = throttle_route_message(600)
check("the routing notice explains why routes are rationed",
      "prices the heat" in route_body)
check("and points at the cheaper thing they can still do",
      "location" in route_body)

# ── pipeline ───────────────────────────────────────────────────────────────

sent: list[tuple[str, str]] = []


async def _capture(to_number, body, **kwargs):
    sent.append((to_number, body))
    return {"messages": [{"id": "wamid.captured"}]}


async def _noop(*args, **kwargs):
    return None


graph_client.send_text = _capture
graph_client.mark_as_read = _noop


def message(index, *, number="14155550123", text=None, location=True):
    return InboundMessage(
        message_id=f"wamid.{number}.{index}",
        from_number=number,
        message_type="location" if location else "text",
        timestamp=None,
        text=text,
        location=RiderLocation(33.4484, -112.0740) if location else None,
        profile_name="Asha",
    )


def run(coro):
    return asyncio.run(coro)


def kinds():
    out = []
    for _, body in sent:
        if "faster than I can usefully check" in body:
            out.append("throttle")
        elif "route comparisons in a short window" in body:
            out.append("route-throttle")
        else:
            out.append("reply")
    return out


print("\n[8] A flooding rider is cut off after one warning")
webhook._message_limiter.reset()
webhook._route_limiter.reset()
hub.clear()

results = []
for i in range(6):
    sent.clear()
    run(webhook.handle_message(message(i)))
    results.append(kinds())

check(f"the first three messages are answered (got {results[:3]})",
      all("reply" in r for r in results[:3]))
check(f"the fourth gets exactly one throttle notice (got {results[3]})",
      results[3] == ["throttle"])
check(f"the fifth gets nothing at all (got {results[4]})", results[4] == [])
check(f"and the sixth too (got {results[5]})", results[5] == [])

print("\n[9] Throttling reaches the dispatcher, once")
# A rider suddenly hammering the service is worth attention: as likely to be
# someone in trouble tapping send as it is abuse.
throttle_events = [e for e in hub.history()
                   if e["kind"] == "system" and "throttled" in e["text"]]
check(f"one system event was published (got {len(throttle_events)})",
      len(throttle_events) == 1)
check("it names the rider", throttle_events and throttle_events[0]["rider_id"])
check("the number is masked like everywhere else",
      throttle_events and "****" in throttle_events[0]["rider_id"])

print("\n[10] One rider's flood does not affect another")
webhook._message_limiter.reset()
hub.clear()
for i in range(5):
    sent.clear()
    run(webhook.handle_message(message(i, number="14155550123")))
check("the first rider is throttled", kinds() == [])
sent.clear()
run(webhook.handle_message(message(0, number="14155557777")))
check(f"a second rider is answered normally (got {kinds()})", "reply" in kinds())

print("\n[11] Routing has its own, tighter budget")
# One comparison prices several grid cells and each cold cell is two billed
# heatmap requests, so a route costs an order of magnitude more than a pin.
webhook._message_limiter.reset()
webhook._route_limiter.reset()
hub.clear()

# Give the rider a known position so routing gets past the origin check, and
# stub the comparison so no OSRM call is attempted.
run(webhook.handle_message(message(0, number="14155551111")))
webhook._message_limiter.reset()

real_compare = webhook.compare_routes
webhook.compare_routes = lambda origin, dest: None   # "no route found"

sent.clear()
run(webhook.handle_message(message(1, number="14155551111",
                                   text="to 33.45,-112.06", location=False)))
first_route = kinds()
sent.clear()
run(webhook.handle_message(message(2, number="14155551111",
                                   text="to 33.46,-112.05", location=False)))
second_route = kinds()
sent.clear()
run(webhook.handle_message(message(3, number="14155551111",
                                   text="to 33.47,-112.04", location=False)))
third_route = kinds()

check(f"the first route request is served (got {first_route})",
      "route-throttle" not in first_route)
check(f"the second is throttled with one notice (got {second_route})",
      second_route == ["route-throttle"])
check(f"the third is silent (got {third_route})", third_route == [])

print("\n[12] The route budget does not close the safety path")
# Someone who has used up their route comparisons must still be able to ask
# whether it is safe to ride. That is the part nobody should ever be rationed
# out of by a cost control.
sent.clear()
run(webhook.handle_message(message(9, number="14155551111")))
check(f"a plain location check still gets a verdict (got {kinds()})",
      "reply" in kinds() and "route-throttle" not in kinds())

webhook.compare_routes = real_compare
webhook._message_limiter.reset()
webhook._route_limiter.reset()
hub.clear()

print("\n[13] A refused request hands its budget back")
# Direct coverage for the seam behind [12]: a message that clears the general
# budget and is then refused by a narrower one must not have spent anything.
lim13 = RateLimiter(2, 60.0)
lim13.check("e", now=T)
check("one hit used of two", lim13.remaining("e", now=T) == 1)
lim13.refund("e")
check("refunding returns it", lim13.remaining("e", now=T) == 2)
lim13.refund("e")
check("refunding an empty history is harmless, not negative",
      lim13.remaining("e", now=T) == 2)
check("refunding an unknown rider does not raise",
      lim13.refund("never-seen") is None)
disabled = RateLimiter(0, 60.0)
check("refund on a disabled limiter is a no-op",
      disabled.refund("f") is None)

print("\n" + ("ALL RATE-LIMIT CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

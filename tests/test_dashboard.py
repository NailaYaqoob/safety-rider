"""Dashboard API: page, state, SSE, and the guarded demo simulator.

Run:  venv/bin/python tests/test_dashboard.py
"""
import json, os, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# The hub now persists its rider registry, so point it at a throwaway file
# BEFORE importing anything — otherwise the suite writes test riders into the
# repo's real data/ directory.
_REGISTRY_DIR = tempfile.mkdtemp(prefix="safety-rider-test-")
os.environ["SAFETY_RIDER_REGISTRY_PATH"] = str(pathlib.Path(_REGISTRY_DIR) / "riders.json")

# Offline and non-sending, pinned BEFORE the app import. See tests/README.md.
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
os.environ["SAFETY_RIDER_DEV_TOOLS"] = "1"
os.environ["SAFETY_RIDER_DEMO_NUMBER"] = ""      # empty => simulate sends nothing
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_APP_SECRET"] = "test-app-secret"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = ""

from fastapi.testclient import TestClient

from safety_rider.app import app
from safety_rider.events import (REGISTRY_PATH, REGISTRY_TTL, Event, Hub,
                                 RiderState, hub, mask_number)

client = TestClient(app)
ok = True

def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


print("\n[1] Page and vendored assets")
r = client.get("/dashboard")
check(f"GET /dashboard -> 200 html (got {r.status_code})",
      r.status_code == 200 and "<title>Safety Rider" in r.text)
for asset in ("leaflet.js", "leaflet.css"):
    a = client.get(f"/dashboard/static/vendor/{asset}")
    check(f"{asset} served locally ({a.status_code}, {len(a.content)} bytes)",
          a.status_code == 200 and len(a.content) > 1000)
check("page references the local vendor copy, not a CDN",
      "/dashboard/static/vendor/leaflet.js" in r.text
      and "unpkg.com/leaflet" not in r.text)

print("\n[2] Phone masking")
check("masks a real number", mask_number("14155550123") == "+14155****123")
check("full number never appears in the mask", "5550123" not in mask_number("14155550123"))
check("short number handled", mask_number("12345") == "+12345")
check("non-numeric label passes through", mask_number("demo") == "demo")

print("\n[3] State endpoint")
hub.clear()
s = client.get("/api/dashboard/state").json()
check("has riders/events/center", {"riders","events","center"} <= set(s))
check(f"centre defaults to San Jose ({s['center']['lat']}, {s['center']['lon']})",
      abs(s["center"]["lat"] - 37.3318) < 0.01)
check("starts empty after clear", s["riders"] == [] and s["events"] == [])
check("reports mock mode so the UI can label it", s["mock_mode"] is True)

# The trend chart draws its threshold lines from these. If they ever drift from
# the banding engine the picture contradicts the colours printed beside it —
# a chart that says "below the line" next to a dot the engine called Danger.
from safety_rider.heat_risk import DANGER_C, OSHA_HIGH_C   # noqa: E402
check("serves the band thresholds", "thresholds" in s)
check(f"high-heat line is the engine's ({s['thresholds']['high_heat_c']})",
      s["thresholds"]["high_heat_c"] == OSHA_HIGH_C)
check(f"danger line is the engine's ({s['thresholds']['danger_c']})",
      s["thresholds"]["danger_c"] == DANGER_C)
check("thresholds are ordered low then high",
      s["thresholds"]["high_heat_c"] < s["thresholds"]["danger_c"])

print("\n[4] Simulate heat spike")
r = client.post("/api/dashboard/simulate",
                json={"temperature_c": 41.5, "send_whatsapp": True})
d = r.json()
check(f"200 (got {r.status_code})", r.status_code == 200)
check(f"reaches DANGER at 41.5 (got {d['status']})", d["status"] == "danger")
check("rest protocol flagged", d["rest_protocol"] is True)
check("no demo number configured -> nothing sent", d["whatsapp_sent"] is False)
check("says why nothing was sent", d["error"] and "DEMO_NUMBER" in d["error"])
check("returns the exact reply text for the demo", "Stop riding" in d["reply_preview"])
check("reply carries SIMULATED provenance", "SIMULATED" in d["reply_preview"])

s = client.get("/api/dashboard/state").json()
check(f"rider appears on the map ({len(s['riders'])})", len(s["riders"]) == 1)
check("rider carries the danger status", s["riders"][0]["status"] == "danger")
check("feed quotes the SAME temperature the rider was sent (41.5, not 42)",
      any("41.5 °C" in e["text"] for e in s["events"]))

print("\n[5] The simulator cannot be turned into a spam relay")
import inspect
from safety_rider.dashboard.routes import SimulateRequest
fields = set(SimulateRequest.model_fields)
check(f"request body has no recipient field ({sorted(fields)})",
      not fields & {"to", "to_number", "recipient", "phone", "number"})

print("\n[6] Dev tools can be switched off")
from safety_rider import config as cfg
import safety_rider.dashboard.routes as droutes
saved = droutes.settings
class _Off:
    def __getattr__(self, n): return getattr(saved, n)
    dev_tools = False
droutes.settings = _Off()
r = client.post("/api/dashboard/simulate", json={"temperature_c": 41.5})
check(f"simulate -> 403 when disabled (got {r.status_code})", r.status_code == 403)
r = client.post("/api/dashboard/reset")
check(f"reset -> 403 when disabled (got {r.status_code})", r.status_code == 403)
droutes.settings = saved

print("\n[7] SSE stream")
# Exercised through the endpoint directly, not TestClient.stream: the generator
# is an infinite loop by contract, so a blocking client read never returns and
# the suite hangs. A stub request lets us drive the disconnect deterministically.
import asyncio
from safety_rider.dashboard.routes import dashboard_stream

class FakeRequest:
    """Reports connected for the first N polls, then disconnected."""
    def __init__(self, polls=2):
        self.polls, self.seen = polls, 0
    async def is_disconnected(self):
        self.seen += 1
        return self.seen > self.polls

async def drive_stream():
    resp = await dashboard_stream(FakeRequest(polls=2))
    frames, it = [], resp.body_iterator
    frames.append(await anext(it))                       # opening frame
    hub.publish(Event(kind="evaluation", text="stream check",
                      status="danger", rider_id="+14155****123",
                      latitude=37.3, longitude=-121.8, temperature_c=41.5))
    frames.append(await anext(it))                       # the published event
    await it.aclose()
    return resp, frames

resp, frames = asyncio.run(drive_stream())
check(f"content-type is text/event-stream (got {resp.headers.get('content-type')})",
      "text/event-stream" in resp.headers.get("content-type", ""))
check("proxy buffering disabled (X-Accel-Buffering: no)",
      resp.headers.get("x-accel-buffering") == "no")
check("caching disabled", "no-cache" in resp.headers.get("cache-control", ""))
check(f"opens with an SSE frame ({frames[0][:24]!r})",
      frames[0].startswith(":") or frames[0].startswith("data:"))
check("published event reaches the stream", "stream check" in frames[1])
check("frames are SSE-terminated (blank line)", frames[1].endswith("\n\n"))
payload = json.loads(frames[1].split("data: ", 1)[1].strip())
check(f"event carries coordinates for the map ({payload['latitude']}, {payload['longitude']})",
      payload["latitude"] == 37.3 and payload["longitude"] == -121.8)
check("event carries the danger status for the blinking marker",
      payload["status"] == "danger")
check("subscriber released after the stream closes", hub.subscriber_count == 0)

print("\n[8] Hub fan-out and backpressure")
h = Hub()
h.upsert_rider(RiderState(rider_id="r1", latitude=1.0, longitude=2.0, status="safe"))
check("rider stored", len(h.riders()) == 1)
h.upsert_rider(RiderState(rider_id="r1", latitude=3.0, longitude=4.0, status="danger"))
check("same id updates in place, no duplicate", len(h.riders()) == 1
      and h.riders()[0]["status"] == "danger")
for i in range(150):
    h.publish(Event(kind="system", text=f"e{i}"))
check(f"history is capped ({len(h.history())} <= 100)", len(h.history()) <= 100)
check("keeps the newest", h.history()[-1]["text"] == "e149")

async def saturate():
    async with h.subscribe() as q:
        check("subscriber registered", h.subscriber_count == 1)
        for i in range(500):          # far beyond QUEUE_MAXSIZE
            h.publish(Event(kind="system", text=f"flood{i}"))
        return h.subscriber_count
left = asyncio.run(saturate())
check(f"a subscriber that never drains is dropped, not allowed to block (count={left})",
      left == 0)
check("hub is empty again after the context exits", h.subscriber_count == 0)

print("\n[9] Rider state serialises cleanly for the browser")
st = RiderState(rider_id="+14155****123", latitude=37.3, longitude=-121.8,
                status="danger", temperature_c=41.5)
js = json.dumps(st.to_dict())
check("JSON round-trips", json.loads(js)["temperature_c"] == 41.5)
check("carries an ISO timestamp", "T" in json.loads(js)["updated_at"])

print("\n[10] The feed never claims a delivery that did not happen")
from safety_rider.models import RiderLocation
from safety_rider.rider_status import evaluate_rider_safety_status
from safety_rider.whatsapp.webhook import publish_evaluation

def feed_text(delivered, temp_c=41.5):
    hub.clear()
    publish_evaluation(
        from_number="14155551234",
        location=RiderLocation(latitude=33.4484, longitude=-112.0740, name="Phoenix"),
        status=evaluate_rider_safety_status(temp_c, hours_above_threshold=10.0),
        reading=None,
        label="Probe",
        delivered=delivered,
    )
    return [e for e in hub.history() if e["kind"] == "evaluation"][-1]["text"]

sent_text = feed_text(True)
check(f"delivered=True says sent -> {sent_text!r}", "sent." in sent_text)

failed_text = feed_text(False)
check("delivered=False does NOT claim it was sent",
      "sent." not in failed_text)
check(f"delivered=False says so plainly -> {failed_text!r}",
      "NOT delivered" in failed_text and "not reached" in failed_text)

none_text = feed_text(None)
check("delivered=None makes no delivery claim at all",
      "sent." not in none_text and "NOT delivered" not in none_text)

check("the reading itself still reaches the feed",
      "41.5" in sent_text and "41.5" in failed_text and "41.5" in none_text)

print("\n[11] The rider registry survives a restart")
# A routing request is answered from the rider's last known position. That used
# to live only in memory, so any redeploy told a rider who pinned thirty
# seconds ago that we had no idea where they were — and Railway redeploys often
# during a hackathon.
from datetime import datetime, timedelta, timezone

fresh = Hub()
fresh.upsert_rider(RiderState(rider_id="+1602****740", latitude=33.4484,
                              longitude=-112.0740, status="danger",
                              temperature_c=41.5))
check("upserting a rider writes the registry to disk", REGISTRY_PATH.exists())

reborn = Hub()
check(f"a new process restores it (got {reborn.load()})", reborn.find_rider("+1602****740") is not None)
restored_rider = reborn.find_rider("+1602****740")
check("position survives intact",
      restored_rider.latitude == 33.4484 and restored_rider.longitude == -112.0740)
check("so does the band it was last evaluated in", restored_rider.status == "danger")

stale_at = (datetime.now(timezone.utc) - REGISTRY_TTL - timedelta(minutes=1)).isoformat()
reborn.upsert_rider(RiderState(rider_id="stale", latitude=1.0, longitude=2.0,
                               updated_at=stale_at))
aged = Hub(); aged.load()
check("a position past the TTL is dropped, not served", aged.find_rider("stale") is None)
check("while a fresh one is kept", aged.find_rider("+1602****740") is not None)
check("and the expired row is purged from disk too",
      "stale" not in REGISTRY_PATH.read_text(encoding="utf-8"))

REGISTRY_PATH.write_text("{ this is not json", encoding="utf-8")
salvaged = Hub()
check("a corrupt registry is a cold boot, not a crash", salvaged.load() == 0)

REGISTRY_PATH.unlink(missing_ok=True)
check("a missing registry is also fine", Hub().load() == 0)

print("\n[12] A route comparison reaches the map, not just the feed")
from safety_rider.routing import RouteCandidate, RouteComparison

hub.clear()
check("no route before one is computed",
      client.get("/api/dashboard/state").json()["route"] is None)

fast = RouteCandidate("Direct", 8000, 1800,
                      coordinates=[[-112.07, 33.44 + i * 0.001] for i in range(300)])
cool = RouteCandidate("Detour 1", 11000, 2400,
                      coordinates=[[-112.09, 33.44 + i * 0.001] for i in range(300)])
fast.peak_c, fast.degree_hours, fast.coverage = 44.2, 6.5, 1.0
cool.peak_c, cool.degree_hours, cool.coverage = 30.2, 0.5, 1.0
comparison = RouteComparison(fast, cool, [fast, cool], True, "saves 6.00 C·h for 10 min extra")

hub.publish(Event(kind="route", text="Rider +1602****740 requested a route.",
                  status="warning", rider_id="+1602****740",
                  route=comparison.to_map_payload()))

state = client.get("/api/dashboard/state").json()
check("a dashboard opened AFTER the comparison still paints it", state["route"] is not None)
check("both legs survive the round trip",
      "fastest" in state["route"] and "coolest" in state["route"])
check("the geometry arrives with them", len(state["route"]["fastest"]["path"]) > 1)
check("the event in history carries it too",
      any(e.get("route") for e in state["events"] if e["kind"] == "route"))
check("evaluations do NOT carry route geometry",
      all(e.get("route") is None for e in state["events"] if e["kind"] != "route"))

hub.clear()
check("reset clears the route with everything else",
      client.get("/api/dashboard/state").json()["route"] is None)

print("\n[13] The heat overlay is served from cache, never billed")
# This is what makes "per-tile verdicts, not a city average" visible instead of
# merely claimed. It is fetched by the browser on a toggle and on every rider,
# so it must not be able to reach the API — an idle tab would spend credits.
import json as _json
from datetime import date as _date, timedelta as _timedelta

from safety_rider.heat_layer import cache_dir, grid_key

r = client.get("/api/dashboard/heat", params={"lat": 33.4484, "lon": -112.0740})
check(f"an un-cached cell is a 404, not a fetch (got {r.status_code})", r.status_code == 404)
check("and explains how the layer gets there",
      "cached" in r.json()["detail"] and "warmer" in r.json()["detail"])

sample = pathlib.Path("data/heatmaps/heatmap_parcel_diridon_san_jose_2024-07-15_tcm.json")
if not sample.exists():
    check("sample layer fixture is present", False)
else:
    lat, lon = 37.3318, -121.8899
    cell_lat, cell_lon = grid_key(lat, lon)
    seeded = (_date.today() - _timedelta(days=3)).isoformat()
    seed_path = cache_dir() / f"heatmap_rider_{cell_lat:.5f}_{cell_lon:.5f}_{seeded}_tcm.json"
    seed_path.write_text(sample.read_text(), encoding="utf-8")
    try:
        r = client.get("/api/dashboard/heat", params={"lat": lat, "lon": lon})
        check(f"a cached cell is served (got {r.status_code})", r.status_code == 200)
        layer = r.json()
        check("as a GeoJSON FeatureCollection", layer["type"] == "FeatureCollection")
        check(f"with every tile ({layer['tiles']})", layer["tiles"] > 0)
        check("the date the tiles describe travels with them",
              layer["observed_date"] == seeded)
        check("so does the range the colour ramp needs",
              layer["min_c"] <= layer["max_c"])
        feature = layer["features"][0]
        check("each tile is a polygon", feature["geometry"]["type"] == "Polygon")
        check("carrying exactly one property — the peak",
              list(feature["properties"]) == ["t"])
        check("coordinates are rounded, not full float precision",
              all(len(str(c).split(".")[-1]) <= 5
                  for c in feature["geometry"]["coordinates"][0][0]))
        check("the response is cacheable — a written layer never changes",
              "max-age" in r.headers.get("cache-control", ""))
        check(f"and it is smaller than the raw response "
              f"({len(r.content)} vs {seed_path.stat().st_size} bytes)",
              len(r.content) < seed_path.stat().st_size)
    finally:
        seed_path.unlink(missing_ok=True)

    # With the seed gone the walk-back continues rather than stopping: this
    # cell may hold genuinely older layers from real runs, and an older date is
    # a correct answer where a stale *seeded* one would not be.
    after = client.get("/api/dashboard/heat", params={"lat": lat, "lon": lon})
    check("with the seed removed it either falls back or reports nothing — "
          f"never re-serves the deleted layer (got {after.status_code})",
          after.status_code == 404 or after.json()["observed_date"] != seeded)

hub.clear()
print("\n" + ("ALL DASHBOARD CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

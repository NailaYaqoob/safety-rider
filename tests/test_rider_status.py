"""The intelligence engine end to end, without touching Meta or FortyGuard.

Covers getHyperlocalTemperature, evaluateRiderSafetyStatus, and the unified
webhook controller that chains them.

Run:  venv/bin/python tests/test_rider_status.py
"""
import hashlib, hmac, json, os, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Pin everything OFFLINE before importing the app. Without these the suite
# picks up a real FORTYGUARD_API_KEY / WHATSAPP_ACCESS_TOKEN from .env and
# makes billable calls. Empty strings rather than pops: an unset name lets
# config.py's load_dotenv() refill it from .env, while an empty value is
# "present" to dotenv and "unset" to config._env().
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_APP_SECRET"] = "test-app-secret"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = ""

from fastapi.testclient import TestClient

from safety_rider.app import app
from safety_rider.rider_status import (
    DANGER_THRESHOLD_C, SafetyStatus, WARNING_THRESHOLD_C,
    evaluate_rider_safety_status, evaluateRiderSafetyStatus,
)
from safety_rider.temperature_service import (
    SOURCE_MOCK, get_hyperlocal_temperature, getHyperlocalTemperature,
)
from safety_rider.whatsapp import graph_client, webhook
from safety_rider.whatsapp.parser import coordinates_from_text

client = TestClient(app)
ok = True

def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


print("\n[1] Band edges — the boundary is where bugs live")
check("aliases point at the same callables",
      evaluateRiderSafetyStatus is evaluate_rider_safety_status
      and getHyperlocalTemperature is get_hyperlocal_temperature)
check(f"thresholds are {WARNING_THRESHOLD_C}/{DANGER_THRESHOLD_C}",
      WARNING_THRESHOLD_C == 35.0 and DANGER_THRESHOLD_C == 40.0)

cases = [
    (10.0,  SafetyStatus.SAFE),
    (34.9,  SafetyStatus.SAFE),     # just under
    (35.0,  SafetyStatus.WARNING),  # inclusive lower edge
    (37.5,  SafetyStatus.WARNING),
    (39.0,  SafetyStatus.WARNING),  # the spec's stated top of Warning
    (39.9,  SafetyStatus.WARNING),  # the gap the spec left open
    (40.0,  SafetyStatus.DANGER),   # inclusive lower edge
    (55.0,  SafetyStatus.DANGER),
]
for temp, expected in cases:
    got = evaluate_rider_safety_status(temp).status
    check(f"{temp:>5} C -> {expected.value:<7} (got {got.value})", got is expected)

print("\n[2] No gap, no overlap across the whole line")
# 601 evaluations at INFO would bury the rest of the run; the per-evaluation
# log line is wanted in production, just not 601 times here.
import logging
_rs_log = logging.getLogger("safety_rider.rider_status")
_rs_log.setLevel(logging.WARNING)
bands = [evaluate_rider_safety_status(t / 10).status for t in range(0, 601)]
_rs_log.setLevel(logging.NOTSET)
check("every temperature 0-60 C lands in a real band",
      SafetyStatus.UNKNOWN not in bands)
check("bands only ever escalate as temperature rises",
      bands == sorted(bands, key=[SafetyStatus.SAFE, SafetyStatus.WARNING,
                                  SafetyStatus.DANGER].index))

print("\n[3] Unknown input degrades, never raises")
for bad in (None, float("nan"), float("inf"), "not a number", object()):
    st = evaluate_rider_safety_status(bad)
    check(f"{bad!r:<20} -> unknown, no exception", st.status is SafetyStatus.UNKNOWN)
check("unknown still gives the rider something to do",
      len(evaluate_rider_safety_status(None).actions) > 0)

print("\n[4] Danger triggers the protocol flags")
danger = evaluate_rider_safety_status(42.0)
check("rest_protocol is set", danger.rest_protocol is True)
check("reroute is set", danger.reroute is True)
check("actions mention stopping", any("Stop riding" in a for a in danger.actions))
warn = evaluate_rider_safety_status(36.0)
check("Warning does NOT set rest_protocol", warn.rest_protocol is False)
check("Warning recommends hydration", any("Drink" in a for a in warn.actions))
safe = evaluate_rider_safety_status(20.0)
check("Safe carries no actions (no nagging)", safe.actions == [])

print("\n[5] Duration promotes Safe, but never fabricates Danger")
check("34 C + 6h sustained -> warning",
      evaluate_rider_safety_status(34.0, hours_above_threshold=6.0).status
      is SafetyStatus.WARNING)
check("34 C + 1h -> stays safe",
      evaluate_rider_safety_status(34.0, hours_above_threshold=1.0).status
      is SafetyStatus.SAFE)
check("38 C + 12h stays WARNING (40 C cutoff is not overridden)",
      evaluate_rider_safety_status(38.0, hours_above_threshold=12.0).status
      is SafetyStatus.WARNING)
check("long exposure adds a timing tip",
      any("before 10am" in a for a in
          evaluate_rider_safety_status(38.0, hours_above_threshold=12.0).actions))

print("\n[6] getHyperlocalTemperature (mock mode — no network)")
r1 = get_hyperlocal_temperature(37.3318, -121.8899)
r2 = get_hyperlocal_temperature(37.3318, -121.8899)
check(f"returns a reading (source={r1.source}, {r1.celsius} C)",
      r1.source == SOURCE_MOCK and r1.ok)
check("deterministic: same point twice -> same value", r1.celsius == r2.celsius)
far = get_hyperlocal_temperature(40.7128, -74.0060)
check(f"different city -> different value ({far.celsius} C)",
      far.celsius != r1.celsius)
check("float(reading) works", float(r1) == r1.celsius)
check(f"fahrenheit conversion ({r1.fahrenheit:.1f} F)",
      abs(r1.fahrenheit - (r1.celsius * 9 / 5 + 32)) < 1e-9)
check("min < mean < peak", r1.daily_min_c < r1.daily_mean_c < r1.celsius)
check("observed_date is set and is NOT today",
      r1.observed_date is not None and r1.observed_date != __import__("datetime").date.today().isoformat())
check("describe() flags the reading as simulated", "SIMULATED" in r1.describe())
check(f"note records why it was simulated ({r1.note!r})", bool(r1.note))

bad = get_hyperlocal_temperature(float("nan"), -121.0)
check("non-finite coords -> unavailable, no exception", not bad.ok)
check("unavailable reading -> unknown status",
      evaluate_rider_safety_status(bad.celsius if bad.ok else None).status
      is SafetyStatus.UNKNOWN)

print("\n[7] Typed coordinates parse; prose does not")
loc = coordinates_from_text("37.3318, -121.8899")
check("'37.3318, -121.8899' parses", loc is not None
      and abs(loc.latitude - 37.3318) < 1e-9 and abs(loc.longitude + 121.8899) < 1e-9)
check("whitespace-separated parses", coordinates_from_text("37.33 -121.88") is not None)
check("prose containing numbers is rejected",
      coordinates_from_text("I'll be there in 5, 10 minutes") is None)
check("out-of-range latitude rejected", coordinates_from_text("991.0, -121.0") is None)
check("empty text -> None", coordinates_from_text("") is None)

print("\n[8] Unified controller: POST -> temperature -> band -> reply")
SENT = []
_real_send = graph_client.send_text
async def _capture(to_number, body, **kw):
    SENT.append((to_number, body))
    return {"messages": [{"id": "wamid.FAKE"}]}
graph_client.send_text = _capture

def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()

def post_location(lat, lon, wamid):
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [
        {"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550001111", "phone_number_id": "106540352242922"},
            "contacts": [{"profile": {"name": "Asha"}, "wa_id": "14155551234"}],
            "messages": [{"from": "14155551234", "id": wamid, "timestamp": "1740000000",
                          "type": "location", "location": {"latitude": lat, "longitude": lon}}]}}]}]}
    raw = json.dumps(payload).encode()
    return client.post("/webhook/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": sign(raw),
                                "Content-Type": "application/json"})

# Force the Danger band so the assertion does not depend on which mock value
# this particular grid cell hashes to.
os.environ["SAFETY_RIDER_MOCK_TEMP_C"] = "41.5"
import importlib
from safety_rider import config as cfg
importlib.reload(cfg)
import safety_rider.temperature_service as tsvc
tsvc.settings = cfg.settings

SENT.clear()
r = post_location(37.3318, -121.8899, "wamid.CTRL1")
check(f"POST acked 200 (got {r.status_code})", r.status_code == 200)
check(f"two messages sent (ack + verdict), got {len(SENT)}", len(SENT) == 2)
verdict = SENT[-1][1] if SENT else ""
check("verdict addressed to the sender", bool(SENT) and SENT[-1][0] == "14155551234")
check("Danger band reached at 41.5 C", "🔴" in verdict)
check("rest protocol text present", "Stop riding" in verdict)
check("temperature quoted in the reply", "41.5 °C" in verdict)
check("provenance line marks it SIMULATED", "SIMULATED" in verdict)

os.environ["SAFETY_RIDER_MOCK_TEMP_C"] = "22.0"
importlib.reload(cfg); tsvc.settings = cfg.settings
SENT.clear()
r = post_location(37.4000, -121.9000, "wamid.CTRL2")
verdict = SENT[-1][1] if SENT else ""
check("Safe band at 22 C", "🟢" in verdict and "Clear to ride" in verdict)
check("Safe reply carries no rest protocol", "Stop riding" not in verdict)

print("\n[9] A failing Graph call must not kill the flow")
async def _boom(to_number, body, **kw):
    SENT.append((to_number, "<raised>"))
    raise graph_client.WhatsAppSendError("Graph 401: token expired")
graph_client.send_text = _boom
SENT.clear()
r = post_location(37.5, -121.95, "wamid.CTRL3")
check(f"webhook still returns 200 when Graph 401s (got {r.status_code})",
      r.status_code == 200)
check("both sends were attempted despite the first failing", len(SENT) == 2)

graph_client.send_text = _real_send
os.environ.pop("SAFETY_RIDER_MOCK_TEMP_C", None)

print("\n[10] Duplicate delivery does not double-reply")
graph_client.send_text = _capture
SENT.clear()
post_location(37.6, -121.7, "wamid.DUP")
first = len(SENT)
post_location(37.6, -121.7, "wamid.DUP")
check(f"same wamid twice -> no extra sends ({first} then {len(SENT)})",
      len(SENT) == first and first > 0)
graph_client.send_text = _real_send

print("\n[11] Outside U.S. coverage is reported, never simulated")
# Mock mode short-circuits before the coverage check, so turn it off and give
# config a key. Out-of-coverage bails before any network call, so this stays
# offline: a live lookup would need an in-coverage point, which we never use.
os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "0"
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "1"
os.environ["FORTYGUARD_API_KEY"] = "test-key-never-sent"
importlib.reload(cfg); tsvc.settings = cfg.settings

abroad = get_hyperlocal_temperature(24.86, 67.00)          # Karachi
check("outside coverage -> source 'unavailable'", abroad.source == "unavailable")
check("outside coverage -> not ok", abroad.ok is False)
check("outside coverage -> flagged permanent", abroad.permanent is True)
check("outside coverage -> error explains why",
      bool(abroad.error) and "United States" in abroad.error)

abroad_status = evaluate_rider_safety_status(
    abroad.celsius if abroad.ok else None,
    hours_above_threshold=abroad.hours_above_threshold,
)
check("outside coverage -> UNKNOWN, not a band",
      abroad_status.status is SafetyStatus.UNKNOWN)
check("outside coverage -> no temperature invented",
      abroad_status.temperature_c is None)
check("outside coverage -> no rest protocol", abroad_status.rest_protocol is False)

abroad_text = abroad_status.to_whatsapp_text(abroad)
check("reply names the reason", "United States" in abroad_text)
check("reply does not offer a pointless retry", "retry" not in abroad_text.lower())
check("reply quotes no temperature", "°C" not in abroad_text)
check("reply is not labelled SIMULATED", "SIMULATED" not in abroad_text)

# A transient failure still gets the retry, which is the distinction the
# `permanent` flag exists to make.
transient = evaluate_rider_safety_status(None).to_whatsapp_text()
check("transient failure still offers a retry", "retry" in transient.lower())

os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
importlib.reload(cfg); tsvc.settings = cfg.settings

print("\n[12] A partial day is detected and stepped over, never reported")
# FortyGuard returns a not-yet-ingested day as a success: one hour's snapshot
# with min == mean == max. Reporting it hands the rider a cool, flat, safe
# looking day. Only the shape gives it away, so assert on the shape.
class _FakeLayer:
    def __init__(self, tile): self._tile = tile
    def lookup(self, lat, lon): return self._tile

PARTIAL = {"min_temperature": 15.89, "average_temperature": 15.89, "max_temperature": 15.89}
COMPLETE = {"min_temperature": 15.87, "average_temperature": 19.25, "max_temperature": 25.52}

asked = []
def _fake_fetch(client, lat, lon, study_date=None, threshold_c=0.0):
    asked.append(study_date)
    tile = COMPLETE if len(asked) >= 3 else PARTIAL
    return _FakeLayer(tile), None

os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "0"
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "1"
os.environ["FORTYGUARD_API_KEY"] = "test-key-never-sent"
importlib.reload(cfg); tsvc.settings = cfg.settings

_real_fetch = tsvc.heat_layer.fetch_layers
_real_client = tsvc.FortyGuardClient
tsvc.heat_layer.fetch_layers = _fake_fetch
tsvc.FortyGuardClient = lambda *a, **k: object()
try:
    stepped = tsvc.get_hyperlocal_temperature(37.3318, -121.8899)
    check("partial days are skipped (3 dates tried)", len(asked) == 3)
    check("dates walk backwards one day at a time",
          asked == sorted(asked, reverse=True) and len(set(asked)) == 3)
    check("step-back returns a live reading", stepped.source == "live")
    check("reported peak is the complete day's", stepped.celsius == 25.52)
    check("reported date is the complete day's", stepped.observed_date == asked[-1])
    check("a partial day's flat value is never reported", stepped.celsius != 15.89)

    # Every day partial -> fall back to simulation, not a flat 15.89 "safe".
    asked.clear()
    def _all_partial(client, lat, lon, study_date=None, threshold_c=0.0):
        asked.append(study_date)
        return _FakeLayer(PARTIAL), None
    tsvc.heat_layer.fetch_layers = _all_partial
    exhausted = tsvc.get_hyperlocal_temperature(37.3318, -121.8899)
    check("exhausting the backfill window does not raise", exhausted is not None)
    check("exhausted window never reports the partial value",
          exhausted.celsius != 15.89)
    check("exhausted window is not labelled live", exhausted.source != "live")
    check("backfill window is bounded", len(asked) == cfg.settings.heat_backfill_days)
finally:
    tsvc.heat_layer.fetch_layers = _real_fetch
    tsvc.FortyGuardClient = _real_client
    os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
    os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
    importlib.reload(cfg); tsvc.settings = cfg.settings

print("\n[13] The nowcast decides the band; the day still describes the block")
# filter_type=1 returns ONE hour, so min == average == max by construction.
# That is the exact shape [12] rejects as a partial day -- if that check ever
# leaks into the hourly path, every valid nowcast is thrown away.
HOURLY = {"min_temperature": 30.9, "average_temperature": 30.9, "max_temperature": 30.9}

os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "0"
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "1"
os.environ["FORTYGUARD_API_KEY"] = "test-key-never-sent"
importlib.reload(cfg); tsvc.settings = cfg.settings

_real_fetch = tsvc.heat_layer.fetch_layers
_real_hourly = tsvc.heat_layer.fetch_hourly_tcm
_real_client = tsvc.FortyGuardClient

def _one_complete_day(client, lat, lon, study_date=None, threshold_c=0.0):
    return _FakeLayer({"min_temperature": 24.1, "average_temperature": 31.0,
                       "max_temperature": 41.2}), _FakeLayer({"value": 10.0})

hours_asked = []
def _hourly_ok(client, lat, lon, study_date=None, hour=0):
    hours_asked.append((study_date, hour))
    return _FakeLayer(HOURLY)

def _hourly_none(client, lat, lon, study_date=None, hour=0):
    hours_asked.append((study_date, hour))
    return None            # hour not ingested -- the empty-layer case

tsvc.heat_layer.fetch_layers = _one_complete_day
tsvc.FortyGuardClient = lambda *a, **k: object()
try:
    tsvc.heat_layer.fetch_hourly_tcm = _hourly_ok
    r = tsvc.get_hyperlocal_temperature(37.3318, -121.8899)
    check("a flat hourly tile is NOT mistaken for a partial day",
          r.now_celsius == 30.9)
    check("the nowcast is timestamped", bool(r.now_observed_at))
    check("the daily peak is kept, not overwritten", r.celsius == 41.2)
    check("duration survives from the daily layer", r.hours_above_threshold == 10.0)
    check("the band decides on now, not the daily peak",
          r.decision_celsius == 30.9)

    st = evaluate_rider_safety_status(
        r.decision_celsius, hours_above_threshold=r.hours_above_threshold)
    text = st.to_whatsapp_text(r)
    check("reply says 'Right now', not 'Peak'",
          "Right now where you are" in text and "Peak air temperature" not in text)
    check("reply quotes the nowcast value", "30.9 °C" in text)
    check("41.2 is never shown as the current temperature", "41.2 °C" not in text)
    check("the duration line names its own (older) date",
          "a day above the high-heat line (measured" in text)
    check("provenance carries both timestamps",
          "now " in r.describe() and "duration " in r.describe())
    check("sustained hours still promote a sub-35 reading",
          st.status is SafetyStatus.WARNING)

    # A nowcast that cannot be had must cost nothing.
    hours_asked.clear()
    tsvc.heat_layer.fetch_hourly_tcm = _hourly_none
    degraded = tsvc.get_hyperlocal_temperature(37.3318, -121.8899)
    check("an unavailable hour never invents one", degraded.now_celsius is None)
    check("the daily reading survives a failed nowcast", degraded.celsius == 41.2)
    check("it falls back to the daily peak for the band",
          degraded.decision_celsius == 41.2)
    check("the reply reverts to peak wording",
          "Peak air temperature" in evaluate_rider_safety_status(
              degraded.decision_celsius).to_whatsapp_text(degraded))
    check("hour lookback is bounded",
          len(hours_asked) == cfg.settings.nowcast_lookback_hours)
    check("hours walk backwards from now",
          [h for _, h in hours_asked] == sorted((h for _, h in hours_asked), reverse=True)
          or len(hours_asked) == 1)

    # The switch must actually switch it off -- it is a billed request per hour.
    hours_asked.clear()
    os.environ["SAFETY_RIDER_NOWCAST"] = "0"
    importlib.reload(cfg); tsvc.settings = cfg.settings
    tsvc.heat_layer.fetch_hourly_tcm = _hourly_ok
    off = tsvc.get_hyperlocal_temperature(37.3318, -121.8899)
    check("SAFETY_RIDER_NOWCAST=0 spends no hourly request", hours_asked == [])
    check("with the nowcast off the band is the daily peak",
          off.now_celsius is None and off.decision_celsius == 41.2)
finally:
    tsvc.heat_layer.fetch_layers = _real_fetch
    tsvc.heat_layer.fetch_hourly_tcm = _real_hourly
    tsvc.FortyGuardClient = _real_client
    os.environ.pop("SAFETY_RIDER_NOWCAST", None)
    os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
    os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
    importlib.reload(cfg); tsvc.settings = cfg.settings


print("\n--- sample Danger reply ---")
os.environ["SAFETY_RIDER_MOCK_TEMP_C"] = "41.5"
importlib.reload(cfg); tsvc.settings = cfg.settings
demo_reading = get_hyperlocal_temperature(37.3318, -121.8899)
print(evaluate_rider_safety_status(
    demo_reading.celsius,
    hours_above_threshold=demo_reading.hours_above_threshold,
).to_whatsapp_text(demo_reading))
os.environ.pop("SAFETY_RIDER_MOCK_TEMP_C", None)

print("\n" + ("ALL ENGINE CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

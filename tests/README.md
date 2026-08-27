# Tests

    ../venv/bin/python tests/test_whatsapp_webhook.py
    ../venv/bin/python tests/test_heat_risk_live.py
    ../venv/bin/python tests/test_rider_status.py
    ../venv/bin/python tests/test_routing.py
    ../venv/bin/python tests/test_dashboard.py
    ../venv/bin/python tests/test_rate_limit.py
    ../venv/bin/python tests/test_warm.py
    ../venv/bin/python tests/test_escalation.py

All eight run standalone — no pytest required — and exit non-zero on any failure.
CI runs the same loop on every push (`.github/workflows/tests.yml`).

| File | Covers |
|---|---|
| `test_whatsapp_webhook.py` | GET verification handshake, `X-Hub-Signature-256` enforcement, payload parsing (location / text / delivery receipts / malformed input), wamid deduplication, risk banding, `/health` |
| `test_heat_risk_live.py` | U.S. coverage gate, AOI geometry and cache-grid snapping, tile lookup, cached-layer reuse, duration-based escalation, `env_params` hot-hour selection |
| `test_rider_status.py` | `getHyperlocalTemperature` (determinism, provenance, non-finite input), `evaluateRiderSafetyStatus` band edges at 34.9/35.0/39.9/40.0, a 0–60 °C sweep proving the bands leave no gap, the Danger rest-protocol flags, the published-threshold citation on every verdict, typed-coordinate parsing, and the unified controller from signed POST through to reply text |
| `test_routing.py` | Perpendicular waypoint generation, cell sampling spread across the route, cumulative-exposure scoring, ranking and the detour-worth-it floor, coverage-weighted exposure and the refusal to recommend a route we could not measure, scoring on the same number the rider is banded on, and the thinned map payload |
| `test_rate_limit.py` | The sliding window itself (budget, recovery, notify-once, per-rider isolation, bounded memory, the disable switch), and the pipeline built on it: a flooding rider cut off after one warning, the throttle reaching the dispatcher, and a refused route handing its general budget back so the safety check stays open |
| `test_warm.py` | The nowcast warmer's schedule: forgiving cell parsing, the guards that keep it switched off unless deliberately enabled, one-cell-at-a-time passes, surviving a failing cell and a failing pass, clean cancellation, and the app starting and stopping it. Every billed call is stubbed — a real warm costs credits and four minutes |
| `test_escalation.py` | What a Danger verdict does beyond the reply: dispatcher escalation and its contents, remembering a rider's destination across later evaluations, the automatic cooler route and the four cases where it correctly stays silent, and the route budget applying to it |
| `test_dashboard.py` | Page and vendored assets, phone masking, the state endpoint, the guarded simulator, SSE fan-out and backpressure, honest delivery reporting, the rider registry surviving a restart (including TTL expiry and a corrupt file), and a route comparison reaching the map |

## Every suite is offline by design

None makes a network call, and that is enforced rather than assumed:

* `test_whatsapp_webhook.py` sets `SAFETY_RIDER_LIVE_HEAT=0` **before importing
  the app**. Without it the suite picks up `FORTYGUARD_API_KEY` from `.env`,
  makes real billable calls, and hangs on the polling loop.
* `test_rider_status.py` sets both `SAFETY_RIDER_LIVE_HEAT=0` and
  `SAFETY_RIDER_MOCK_TEMPERATURE=1` before importing the app, and swaps
  `graph_client.send_text` for a capture function so the controller's replies
  are asserted on rather than delivered. It restores the real function after.
* `test_heat_risk_live.py` points `FORTYGUARD_BASE_URL` at an unroutable address
  and injects a `FakeClient` that raises if the code tries to bill a heatmap it
  should have served from cache. It seeds and then removes its own cache files.

`test_routing.py` points OSRM at an unroutable address on a 2 s timeout, so a
test that reaches the network fails fast instead of hanging on a real call.

If you add a test that touches `check_rider_heat_risk` or
`get_hyperlocal_temperature`, keep the same guards.

## One more guard: the rider registry

The hub now persists rider positions to disk. Every suite that drives it sets
`SAFETY_RIDER_REGISTRY_PATH` to a throwaway directory **before importing the
app** — without it the suites write test riders into the repo's real `data/`
directory, and a subsequent run starts with them already loaded.

## Demoing a specific band

`SAFETY_RIDER_MOCK_TEMP_C` pins the simulated temperature to an exact value, so
you can show the Danger branch on a mild afternoon:

    SAFETY_RIDER_MOCK_TEMPERATURE=1 SAFETY_RIDER_MOCK_TEMP_C=41.5 \
        ../venv/bin/uvicorn safety_rider.app:app --port 8000

Without the override the value is derived by hashing the grid cell and date —
deterministic, so the same pin always gives the same answer, but which band a
given pin lands in is arbitrary.

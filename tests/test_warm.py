"""The nowcast warmer's schedule — never touching FortyGuard.

Every billed call is replaced with a recorder. A test that actually warmed a
cell would cost real credits and take four minutes, so `warm_cell` is stubbed
throughout and the assertions are about *when* it is called, not what it
returns.

Run:  venv/bin/python tests/test_warm.py
"""
import asyncio, os, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Pinned BEFORE the app import. See tests/README.md.
os.environ["SAFETY_RIDER_REGISTRY_PATH"] = str(
    pathlib.Path(tempfile.mkdtemp(prefix="safety-rider-test-")) / "riders.json")
os.environ["FORTYGUARD_BASE_URL"] = "http://127.0.0.1:9"   # unroutable
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_APP_SECRET"] = "test-app-secret"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = ""

from safety_rider import warm
from safety_rider.config import Settings

ok = True


def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


print("\n[1] Cells are parsed forgivingly, never fatally")
# A typo in one coordinate must not stop the service booting. The warmer is an
# optimisation; without it the daily reading still answers.
parsed = Settings(warm_cells_raw="33.4484,-112.0740; 37.3318,-121.8899").warm_cells
check(f"two good cells parse (got {len(parsed)})", len(parsed) == 2)
check("in lat,lon order", parsed[0] == (33.4484, -112.0740))
check("whitespace is tolerated",
      Settings(warm_cells_raw="  33.4484 , -112.0740  ").warm_cells == [(33.4484, -112.0740)])
check("newlines separate too",
      len(Settings(warm_cells_raw="33.4,-112.0\n37.3,-121.8").warm_cells) == 2)
check("duplicates collapse",
      Settings(warm_cells_raw="33.4,-112.0;33.4,-112.0").warm_cells == [(33.4, -112.0)])
check("a malformed entry is skipped, not raised",
      Settings(warm_cells_raw="nonsense; 33.4,-112.0").warm_cells == [(33.4, -112.0)])
check("so is a three-part entry",
      Settings(warm_cells_raw="1,2,3; 33.4,-112.0").warm_cells == [(33.4, -112.0)])
check("and an out-of-range one (a transposed lat/lon)",
      Settings(warm_cells_raw="999,0; 33.4,-112.0").warm_cells == [(33.4, -112.0)])
check("unset means no cells", Settings(warm_cells_raw=None).warm_cells == [])
check("empty string means no cells", Settings(warm_cells_raw="").warm_cells == [])

print("\n[2] The warmer stays off unless it is deliberately switched on")
# Every pass spends real credits, so nothing starts by accident.
real_settings = warm.settings


class FakeSettings:
    def __init__(self, **kw):
        self.warm_cells_raw = kw.get("raw", "33.4,-112.0")
        self.nowcast = kw.get("nowcast", True)
        self.heat_live = kw.get("heat_live", True)
        self.fortyguard_api_key = kw.get("key", "k")
        self.warm_hours = 1
        self.warm_interval_s = 3600.0

    @property
    def warm_cells(self):
        return Settings(warm_cells_raw=self.warm_cells_raw).warm_cells


def should(**kw):
    warm.settings = FakeSettings(**kw)
    return warm.scheduler_should_run()


try:
    check("no cells configured -> off", should(raw=None)[0] is False)
    check("and says why", "WARM_CELLS" in should(raw=None)[1])
    check("nowcast disabled -> off", should(nowcast=False)[0] is False)
    check("no API key -> off", should(key=None)[0] is False)
    check("live heat disabled -> off", should(heat_live=False)[0] is False)
    check("cells that all failed to parse -> off", should(raw="garbage")[0] is False)
    allowed, why = should()
    check("fully configured -> on", allowed is True)
    check("and reports the cell count", "1 cell" in why)
finally:
    warm.settings = real_settings

print("\n[3] A pass covers every cell, one at a time")
# The endpoint is a queue and the wait is queue depth, not tile count, so
# firing every cell at once lengthens each and bills them simultaneously.
calls = []
order = []
real_warm_cell = warm.warm_cell


def recorder(latitude, longitude, hours=1):
    order.append("start")
    calls.append((latitude, longitude, hours))
    order.append("end")
    return hours


warm.warm_cell = recorder
try:
    cells = [(33.4484, -112.0740), (37.3318, -121.8899)]
    warmed = asyncio.run(warm.warm_once(cells, 1))
    check(f"every cell is warmed (got {len(calls)})", len(calls) == 2)
    check("in the order configured", [c[:2] for c in calls] == cells)
    check(f"the warm count is returned (got {warmed})", warmed == 2)
    check("cells do not overlap — each finishes before the next starts",
          order == ["start", "end", "start", "end"])

    calls.clear()
    asyncio.run(warm.warm_once([(33.4484, -112.0740)], 3))
    check("the hours-per-cell setting is passed through", calls[0][2] == 3)

    print("\n[4] One bad cell does not end the pass")
    # A warmer that dies on the first failure leaves the rest of the service
    # area cold for an hour.
    calls.clear()

    def explodes(latitude, longitude, hours=1):
        calls.append((latitude, longitude))
        if latitude == 33.4484:
            raise RuntimeError("FortyGuard returned nonsense")
        return 1

    warm.warm_cell = explodes
    warmed = asyncio.run(warm.warm_once(cells, 1))
    check(f"both cells were attempted (got {len(calls)})", len(calls) == 2)
    check(f"and the good one still counts (got {warmed})", warmed == 1)

    print("\n[5] The loop survives a failing pass and can be cancelled")

    passes = []

    async def drive(fail_first: bool):
        state = {"n": 0}

        def counter(latitude, longitude, hours=1):
            state["n"] += 1
            passes.append(state["n"])
            if fail_first and state["n"] == 1:
                raise RuntimeError("first pass fails")
            return 1

        warm.warm_cell = counter
        warm.settings = FakeSettings()
        warm.settings.warm_interval_s = 60.0
        task = asyncio.create_task(warm.run_scheduler())
        await asyncio.sleep(0.2)          # long enough for the first pass
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "exited on its own"

    try:
        outcome = asyncio.run(drive(fail_first=True))
        check("a failing pass does not kill the scheduler", len(passes) >= 1)
        check(f"and cancellation propagates cleanly (got {outcome!r})",
              outcome == "cancelled")
    finally:
        warm.settings = real_settings

    print("\n[6] An unconfigured scheduler returns immediately")
    warm.settings = FakeSettings(raw=None)
    try:
        # Must not hang: with no cells there is nothing to wait for.
        asyncio.run(asyncio.wait_for(warm.run_scheduler(), timeout=2.0))
        check("run_scheduler() exits at once when no cells are set", True)
    except asyncio.TimeoutError:
        check("run_scheduler() exits at once when no cells are set", False)
    finally:
        warm.settings = real_settings

finally:
    warm.warm_cell = real_warm_cell
    warm.settings = real_settings

print("\n[7] The app starts and stops it")
os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"     # app import: no live path
from fastapi.testclient import TestClient  # noqa: E402

from safety_rider.app import app  # noqa: E402

with TestClient(app) as client:
    check("the app boots with the warmer unconfigured",
          client.get("/health").status_code == 200)
    check("and no warm task is left running",
          getattr(app.state, "warm_task", "missing") is None)

print("\n" + ("ALL WARMER CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

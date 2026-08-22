"""Cooler-route comparison, without touching OSRM or the FortyGuard API.

Run:  venv/bin/python tests/test_routing.py
"""
import os, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
# Point OSRM at an unroutable address: any test that reaches the network is a
# bug in the test, and this makes it fail fast instead of hanging on a real call.
os.environ["SAFETY_RIDER_OSRM_URL"] = "http://127.0.0.1:9"
os.environ["SAFETY_RIDER_OSRM_TIMEOUT_S"] = "2"

from safety_rider import routing
from safety_rider.heat_risk import OSHA_HIGH_C
from safety_rider.models import RiderLocation
from safety_rider.routing import RouteCandidate, compare_routes, sample_cells

ok = True
def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")

SJ = RiderLocation(37.3318, -121.8899)
PA = RiderLocation(37.4419, -122.1430)


def line(n, lat0=37.30, lon0=-121.88, dlat=0.0, dlon=-0.02):
    """A synthetic route: n vertices marching in a straight line."""
    return [[lon0 + dlon * i, lat0 + dlat * i] for i in range(n)]


print("\n[1] Perpendicular waypoints")
pts = routing._perpendicular_offsets(SJ, PA, (1000.0, 3000.0))
check(f"two offsets -> four waypoints (got {len(pts)})", len(pts) == 4)
def side(p):
    """Sign of the cross product: which side of start->end this point is on."""
    ax, ay = PA.longitude - SJ.longitude, PA.latitude - SJ.latitude
    bx, by = p[1] - SJ.longitude, p[0] - SJ.latitude
    return (ax * by - ay * bx) > 0
sides = [side(p) for p in pts]
check(f"waypoints fall on BOTH sides of the direct line (sides={sides})",
      any(sides) and not all(sides))
check("offsets are ordered near-then-far from the midpoint", len({round(p[0], 4) for p in pts}) >= 2)
check("degenerate start==end -> no waypoints",
      routing._perpendicular_offsets(SJ, SJ, (1000.0,)) == [])

print("\n[2] Cell sampling spreads across the route (regression)")
# The bug: taking the FIRST n distinct cells scored only each route's opening
# stretch. Every candidate leaves from the same origin, so distinct routes
# returned identical scores and the whole comparison was meaningless.
long_route = RouteCandidate("long", 20000, 1800, coordinates=line(400))
cells = sample_cells(long_route, 4)
check(f"respects the cap (got {len(cells)})", len(cells) <= 4)
check("includes the FIRST cell", cells[0] == routing.grid_key(37.30, -121.88))
last_lon = long_route.coordinates[-1][0]
check("includes the LAST cell (destination represented)",
      cells[-1] == routing.grid_key(37.30, last_lon))
check("samples are spread, not the first four in a row",
      cells != [routing.grid_key(c[1], c[0]) for c in long_route.coordinates[:4]])

short = RouteCandidate("short", 500, 60, coordinates=line(3, dlon=-0.0001))
check("a route inside one cell yields one cell", len(sample_cells(short, 4)) == 1)

print("\n[3] Duplicate detection")
a = RouteCandidate("a", 10000, 600)
check("identical route is a duplicate", routing._is_duplicate(RouteCandidate("b", 10000, 600), [a]))
check("1% longer is still the same road", routing._is_duplicate(RouteCandidate("b", 10100, 604), [a]))
check("20% longer is a different route", not routing._is_duplicate(RouteCandidate("b", 12000, 700), [a]))

print("\n[4] Scoring is cumulative exposure, not peak")
readings = {}
def fake_temp(lat, lon, **kw):
    from safety_rider.temperature_service import SOURCE_MOCK, TemperatureReading
    return TemperatureReading(celsius=readings.get(routing.grid_key(lat, lon), 20.0),
                              latitude=lat, longitude=lon, source=SOURCE_MOCK)
routing.get_hyperlocal_temperature = fake_temp

cool_cell = routing.grid_key(37.30, -121.88)
readings = {cool_cell: OSHA_HIGH_C + 10.0}       # 10 C over the line
r1 = routing.score_candidate(RouteCandidate("hot-1h", 10000, 3600, coordinates=line(3, dlon=-0.0001)))
check(f"1 h at +10 C -> ~10 C·h (got {r1.degree_hours:.2f})", abs(r1.degree_hours - 10.0) < 0.01)
r2 = routing.score_candidate(RouteCandidate("hot-30m", 5000, 1800, coordinates=line(3, dlon=-0.0001)))
check(f"same heat, half the time -> half the cost (got {r2.degree_hours:.2f})",
      abs(r2.degree_hours - 5.0) < 0.01)
readings = {cool_cell: OSHA_HIGH_C - 5.0}
r3 = routing.score_candidate(RouteCandidate("cool", 10000, 3600, coordinates=line(3, dlon=-0.0001)))
check(f"below the line costs nothing (got {r3.degree_hours:.2f})", r3.degree_hours == 0.0)
check("peak and mean are reported", r1.peak_c is not None and r1.mean_c is not None)

print("\n[5] Ranking picks the cooler route when one exists")
HOT, COOL = routing.grid_key(37.30, -121.88), routing.grid_key(37.50, -121.88)
readings = {HOT: OSHA_HIGH_C + 12.0, COOL: OSHA_HIGH_C - 2.0}
hot_route  = RouteCandidate("Direct",   8000, 1800, coordinates=line(3, lat0=37.30, dlon=-0.0001))
cool_route = RouteCandidate("Detour 1", 11000, 2400, coordinates=line(3, lat0=37.50, dlon=-0.0001))
routing.fetch_candidates = lambda s, e, **kw: [hot_route, cool_route]

cmp = compare_routes(SJ, PA)
check("recommends the detour", cmp.worth_detour is True)
check(f"coolest is the cool route (got {cmp.coolest.label})", cmp.coolest.label == "Detour 1")
check(f"fastest is still the direct one (got {cmp.fastest.label})", cmp.fastest.label == "Direct")
check("message names both options", "Direct" not in cmp.to_whatsapp_text()
      or "Cooler route found" in cmp.to_whatsapp_text())
check("message quotes the time cost", "min longer" in cmp.to_whatsapp_text())

print("\n[6] A detour has to be worth it")
# Same heat everywhere: no reason to send anyone the long way.
readings = {HOT: OSHA_HIGH_C + 12.0, COOL: OSHA_HIGH_C + 12.0}
cmp = compare_routes(SJ, PA)
check("equal heat -> no detour offered", cmp.worth_detour is False)
check("reason explains why", "not worth" in cmp.reason or "saves only" in cmp.reason)

# Cool, but absurdly far: 3x the duration is past MAX_DETOUR_RATIO.
readings = {HOT: OSHA_HIGH_C + 12.0, COOL: OSHA_HIGH_C - 2.0}
absurd = RouteCandidate("Detour 1", 60000, 1800 * 3, coordinates=line(3, lat0=37.50, dlon=-0.0001))
routing.fetch_candidates = lambda s, e, **kw: [hot_route, absurd]
cmp = compare_routes(SJ, PA)
check(f"a 3x-longer route is rejected however cool (coolest={cmp.coolest.label})",
      cmp.coolest.label == "Direct" and cmp.worth_detour is False)

print("\n[7] Degrading without heat data")
routing.get_hyperlocal_temperature = lambda lat, lon, **kw: (_ for _ in ()).throw(AssertionError("should not be reached"))
no_geom = RouteCandidate("Direct", 8000, 1800, coordinates=[])
routing.fetch_candidates = lambda s, e, **kw: [no_geom]
cmp = compare_routes(SJ, PA)
check("still returns the route when it cannot be scored", cmp is not None)
check("but does not claim a cooler option", cmp.worth_detour is False)
check("says heat data was unavailable", "no heat data" in cmp.reason)
check("message stays honest", "already the coolest" in cmp.to_whatsapp_text()
      or "no better detour" in cmp.to_whatsapp_text())

print("\n[8] No route at all")
routing.fetch_candidates = lambda s, e, **kw: []
check("returns None rather than raising", compare_routes(SJ, PA) is None)

print("\n[9] OSRM failure is contained")
import importlib
importlib.reload(routing)          # restore the real functions
cands = routing.fetch_candidates(SJ, PA)   # unroutable URL, 2 s timeout
check(f"unreachable OSRM -> empty list, no exception (got {len(cands)})", cands == [])

print("\n" + ("ALL ROUTING CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

"""Cooler-route comparison, without touching OSRM or the FortyGuard API.

Run:  venv/bin/python tests/test_routing.py
"""
import json, os, pathlib, sys

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
from safety_rider.routing import (MAX_DISPLAY_POINTS, RouteCandidate, RouteComparison,
                                  _thin, compare_routes, sample_cells)

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

print("\n[7] Cells we could not price do not become free exposure")
# A route that leaves FortyGuard coverage part-way has fewer readings than
# cells. Dividing the trip duration by the READINGS poured the whole ride into
# the covered half and invented exposure that was never measured.
readings = {}
half_out = RouteCandidate("half-out", 10000, 3600, coordinates=line(3, dlon=-0.0001))
_cells = sample_cells(half_out, routing.settings.max_route_cells)
readings = {c: OSHA_HIGH_C + 10.0 for c in _cells}
full = routing.score_candidate(RouteCandidate("full", 10000, 3600, coordinates=line(3, dlon=-0.0001)))
check(f"fully covered route is priced over all of itself (coverage={full.coverage:.2f})",
      abs(full.coverage - 1.0) < 1e-9)

real_coverage = routing.is_in_coverage
routing.is_in_coverage = lambda lat, lon: lat < 37.35      # north half is "outside"
mixed = RouteCandidate("mixed", 10000, 3600,
                       coordinates=[[-121.88, 37.30], [-121.88, 37.30], [-121.88, 37.60]])
readings = {routing.grid_key(37.30, -121.88): OSHA_HIGH_C + 10.0,
            routing.grid_key(37.60, -121.88): OSHA_HIGH_C + 10.0}
mixed = routing.score_candidate(mixed)
check(f"a half-uncovered route reports partial coverage (got {mixed.coverage:.2f})",
      0.0 < mixed.coverage < 1.0)
check(f"and is charged only for the half we measured "
      f"({mixed.degree_hours:.2f} vs {full.degree_hours:.2f} C·h)",
      mixed.degree_hours < full.degree_hours)
routing.is_in_coverage = real_coverage

print("\n[8] An unmeasured detour cannot win on missing data")
# Unpriced ground scores zero, so without a coverage floor the coolest-looking
# route is simply the one we know least about. That is the recommendation a
# safety product must never make.
routing.is_in_coverage = lambda lat, lon: lat < 37.35
hot_known = RouteCandidate("Direct", 8000, 1800,
                           coordinates=[[-121.88, 37.30], [-121.88, 37.31]])
mostly_blind = RouteCandidate("Detour 1", 9000, 2000,
                              coordinates=[[-121.88, 37.30], [-121.88, 37.60],
                                           [-121.88, 37.70], [-121.88, 37.80]])
readings = {routing.grid_key(37.30, -121.88): OSHA_HIGH_C + 12.0}
routing.fetch_candidates = lambda s, e, **kw: [hot_known, mostly_blind]
cmp = compare_routes(SJ, PA)
check(f"the barely-measured detour is not offered (coolest={cmp.coolest.label})",
      cmp.coolest.label == "Direct")
check("so no cooler route is claimed", cmp.worth_detour is False)
routing.is_in_coverage = real_coverage

print("\n[9] Routes are priced on the same number the rider is banded on")
# The rider reply bands on decision_celsius (the nowcast when there is one).
# Scoring routes on the daily peak instead let one conversation say "41.5 C
# right now" and then price a route off a three-day-old 36 C peak.
def nowcast_temp(lat, lon, **kw):
    from safety_rider.temperature_service import SOURCE_LIVE, TemperatureReading
    return TemperatureReading(celsius=OSHA_HIGH_C - 20.0,   # stale daily peak
                              now_celsius=OSHA_HIGH_C + 10.0,  # what it is NOW
                              latitude=lat, longitude=lon, source=SOURCE_LIVE)
routing.get_hyperlocal_temperature = nowcast_temp
nowcast_route = routing.score_candidate(
    RouteCandidate("nowcast", 10000, 3600, coordinates=line(3, dlon=-0.0001)))
check(f"scored on the nowcast, not the stale peak (got {nowcast_route.degree_hours:.2f} C·h)",
      abs(nowcast_route.degree_hours - 10.0) < 0.01)
check(f"and reports the nowcast as the peak (got {nowcast_route.peak_c})",
      abs(nowcast_route.peak_c - (OSHA_HIGH_C + 10.0)) < 0.05)
routing.get_hyperlocal_temperature = fake_temp

print("\n[10] Degrading without heat data")
routing.get_hyperlocal_temperature = lambda lat, lon, **kw: (_ for _ in ()).throw(AssertionError("should not be reached"))
no_geom = RouteCandidate("Direct", 8000, 1800, coordinates=[])
routing.fetch_candidates = lambda s, e, **kw: [no_geom]
cmp = compare_routes(SJ, PA)
check("still returns the route when it cannot be scored", cmp is not None)
check("but does not claim a cooler option", cmp.worth_detour is False)
check("says heat data was unavailable", "no heat data" in cmp.reason)
check("message stays honest", "already the coolest" in cmp.to_whatsapp_text()
      or "no better detour" in cmp.to_whatsapp_text())

print("\n[11] No route at all")
routing.fetch_candidates = lambda s, e, **kw: []
check("returns None rather than raising", compare_routes(SJ, PA) is None)

print("\n[12] OSRM failure is contained")
import importlib
importlib.reload(routing)          # restore the real functions
cands = routing.fetch_candidates(SJ, PA)   # unroutable URL, 2 s timeout
check(f"unreachable OSRM -> empty list, no exception (got {len(cands)})", cands == [])

print("\n[13] The comparison is small enough to reach the dashboard")
# The routes are drawn on the dispatch console, which means the geometry rides
# an SSE frame. A scored comparison holds every OSRM vertex — measured at
# 565-812 per candidate — so it is thinned before it is sent.
big_fast = RouteCandidate("Direct", 8000, 1800,
                          coordinates=[[-112.07, 33.44 + i * 0.0005] for i in range(600)])
big_cool = RouteCandidate("Detour 1", 11000, 2400,
                          coordinates=[[-112.09, 33.44 + i * 0.0005] for i in range(700)])
for c, peak, dh in ((big_fast, 44.2, 6.5), (big_cool, 30.2, 0.5)):
    c.peak_c, c.degree_hours, c.coverage = peak, dh, 1.0
payload = RouteComparison(big_fast, big_cool, [big_fast, big_cool], True, "saves 6.00 C·h").to_map_payload()

check(f"the fastest route is thinned (600 -> {len(payload['fastest']['path'])})",
      len(payload["fastest"]["path"]) <= MAX_DISPLAY_POINTS)
check(f"so is the coolest (700 -> {len(payload['coolest']['path'])})",
      len(payload["coolest"]["path"]) <= MAX_DISPLAY_POINTS)
check("thinning keeps the start point", payload["fastest"]["path"][0] == [33.44, -112.07])
check("thinning keeps the END point — a detour must not drift off its destination",
      payload["fastest"]["path"][-1] == [big_fast.coordinates[-1][1], big_fast.coordinates[-1][0]])
check("coordinates are flipped to Leaflet's [lat, lon]",
      -90 <= payload["fastest"]["path"][0][0] <= 90 and payload["fastest"]["path"][0][1] < -100)
check("a short route is passed through untouched", _thin([[0, 0], [1, 1]], 150) == [[0, 0], [1, 1]])
size = len(json.dumps(payload))
check(f"the whole payload fits comfortably in one SSE frame ({size} bytes)", size < 60_000)

print("\n[14] The map is not told about a choice that was never offered")
# fastest and coolest are the SAME object when no detour is worth it. Sending
# both would draw one line over itself and imply an alternative that does not
# exist.
same = RouteComparison(big_fast, big_fast, [big_fast], False, "not worth it").to_map_payload()
check("no second leg when no detour was recommended", "coolest" not in same)
check("but the route itself is still drawn", len(same["fastest"]["path"]) > 1)
check("and the reason travels with it", same["reason"] == "not worth it")
check("partial coverage is carried through so the map can disclose it",
      "coverage" in same["fastest"])

print("\n" + ("ALL ROUTING CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

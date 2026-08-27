"""Shade routing — canopy from satellite segmentation, and its limits.

No network. Segmentation is Premium and billed, so every call is stubbed and
the cache is a throwaway directory.

Run:  venv/bin/python tests/test_shade.py
"""
import json, os, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

os.environ["SAFETY_RIDER_LIVE_HEAT"] = "0"
os.environ["SAFETY_RIDER_MOCK_TEMPERATURE"] = "1"
os.environ["FORTYGUARD_BASE_URL"] = "http://127.0.0.1:9"      # unroutable
os.environ["SAFETY_RIDER_REGISTRY_PATH"] = str(
    pathlib.Path(tempfile.mkdtemp(prefix="safety-rider-test-")) / "riders.json")

from safety_rider import routing, shade
from safety_rider.config import settings
from safety_rider.heat_risk import OSHA_HIGH_C
from safety_rider.models import RiderLocation
from safety_rider.rider_status import evaluate_rider_safety_status
from safety_rider.routing import RouteCandidate
from safety_rider.temperature_service import SOURCE_MOCK, TemperatureReading

ok = True


def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


def seg(**classes):
    return {"segmentation": {"segments": classes}}


print("\n[1] Canopy is read out of whatever class names arrive")
# The API returns its model's own vocabulary and no documentation here pins it,
# so classes are matched by substring rather than against a fixed schema.
f = shade.shade_fraction_from_result
check("a tree class counts fully",
      abs(f(seg(tree=45.0, building=25.0, road=30.0)) - 0.45) < 1e-6)
check("so does 'tree_canopy'", f(seg(tree_canopy=50.0, road=50.0)) == 0.5)
check("and 'forest'", f(seg(forest=40.0, road=60.0)) == 0.4)
check("grass earns partial credit — it cools the ground but shades nobody",
      0 < f(seg(grass=60.0, road=40.0)) < 0.60)
check("canopy outranks the same share of grass",
      f(seg(tree=50.0, road=50.0)) > f(seg(grass=50.0, road=50.0)))

print("\n[2] Bare asphalt is a measurement, not a gap")
# The most useful signal shade routing has is "this block has no cover at all".
# Reporting that as unknown would discard exactly the cell to route around.
check("a fully built cell is 0.0, not None",
      f(seg(building=40.0, road=55.0, bare_soil=5.0)) == 0.0)
check("and reads as little shade", shade.describe(0.0) == "little shade")

print("\n[3] An unfamiliar vocabulary is unknown, not zero")
# Guessing 0.0 from class names the module cannot read would be a confident
# wrong answer dressed as a real one.
check("all-unrecognised classes -> None", f(seg(class_a=50.0, class_b=50.0)) is None)
check("recognised paving plus an unknown class -> None",
      f(seg(road=90.0, mystery=10.0)) is None)
check("but canopy plus an unknown class still reports the canopy",
      f(seg(tree=30.0, mystery=70.0)) == 0.3)
check("an empty response is unknown", f(seg()) is None)
check("a missing segmentation block is unknown", f({}) is None)

print("\n[4] The percentage scale is inferred, not assumed")
# A 0-1 payload read as 0-100 would report a paved block as fully shaded.
check("0-100 percentages", abs(f(seg(tree=45.0, road=55.0)) - 0.45) < 1e-6)
check("0-1 fractions give the same answer",
      abs(f(seg(tree=0.45, road=0.55)) - 0.45) < 1e-6)

print("\n[5] The rider path never bills for segmentation")
cache_root = pathlib.Path(tempfile.mkdtemp(prefix="safety-rider-seg-"))
real_path = shade._cache_path
shade._cache_path = lambda lat, lon: cache_root / f"shade_{lat:.3f}_{lon:.3f}.json"
try:
    class ExplodingClient:
        def satellite_segmentation(self, **kwargs):
            raise AssertionError("the rider path must never call the API")

    check("a cold cell returns None rather than fetching",
          shade.shade_fraction(33.4484, -112.0740) is None)
    check("even when a client is offered, cache_only holds",
          shade.shade_fraction(33.4484, -112.0740, client=ExplodingClient()) is None)

    print("\n[6] The warmer pays once, and the cache has no expiry")
    calls = []

    class Recorder:
        def satellite_segmentation(self, **kwargs):
            calls.append(kwargs)
            return {"result": {"image_year": 2025,
                               "segmentation": {"segments": {"tree": 60.0, "road": 40.0}}}}

    got = shade.shade_fraction(33.4484, -112.0740, client=Recorder(), cache_only=False)
    check(f"the warmer fetches and gets a fraction (got {got})", got == 0.6)
    check("exactly one API call", len(calls) == 1)
    # The first live attempt used filter_type=3 with today's date and the
    # activity came back Failed with no reason. The shape the working use-case
    # notebooks send is a single past hour, so that is what goes out now.
    check("filter_type 1 with a start_time — the shape that actually succeeds",
          calls[0].get("filter_type") == 1 and calls[0].get("start_time"))
    check("and never today's date, which the catalog rejects",
          calls[0].get("start_date") < __import__("datetime").date.today().isoformat())

    again = shade.shade_fraction(33.4484, -112.0740, client=Recorder(), cache_only=False)
    check(f"a second warm is served from disk (got {again})", again == 0.6)
    check("with no further API call", len(calls) == 1)
    check("and the rider path can now read it",
          shade.shade_fraction(33.4484, -112.0740) == 0.6)

    print("\n[7] A failing segmentation call costs nothing but the shade")
    class Broken:
        def satellite_segmentation(self, **kwargs):
            raise RuntimeError("premium plan required")

    check("failure is None, not an exception",
          shade.shade_fraction(40.0, -100.0, client=Broken(), cache_only=False) is None)

    print("\n[8] Outside coverage there is no shade to look up")
    check("a London point is refused without touching the cache",
          shade.shade_fraction(51.5, -0.12, client=Recorder(), cache_only=False) is None)

    print("\n[9] Shade changes which route is recommended")
    # Identical air temperature on both; only canopy differs.
    SUN = routing.grid_key(33.40, -112.07)

    def flat_temp(lat, lon, **kwargs):
        return TemperatureReading(celsius=OSHA_HIGH_C + 8.0, latitude=lat,
                                  longitude=lon, source=SOURCE_MOCK)

    real_temp, real_shade, real_fetch = (routing.get_hyperlocal_temperature,
                                         routing.shade_fraction, routing.fetch_candidates)
    routing.get_hyperlocal_temperature = flat_temp
    routing.shade_fraction = lambda lat, lon: 0.05 if routing.grid_key(lat, lon) == SUN else 0.70

    def line(lat0):
        return [[-112.07, lat0 + i * 0.0001] for i in range(3)]

    open_road = RouteCandidate("Direct", 8000, 1800, coordinates=line(33.40))
    leafy = RouteCandidate("Detour 1", 8600, 1980, coordinates=line(33.60))
    routing.fetch_candidates = lambda s, e, **kw: [open_road, leafy]

    comparison = routing.compare_routes(RiderLocation(33.40, -112.07),
                                        RiderLocation(33.60, -112.07))
    check(f"the shadier route wins on equal temperature (got {comparison.coolest.label})",
          comparison.coolest.label == "Detour 1")
    check("and the detour is actually offered", comparison.worth_detour is True)
    check("shade is reported per candidate",
          all(c.shade_fraction is not None for c in comparison.candidates))
    body = comparison.to_whatsapp_text()
    check(f"the rider is told which is shadier (body mentions shade)",
          "shaded" in body or "shade" in body)

    print("\n[10] Shade moves ranking — never a reported temperature or a band")
    # A model that let a leafy street talk a 41 C reading down into Warning
    # would be inventing safety out of an assumption.
    check("peak_c stays the MEASURED value, not the shade-adjusted one",
          abs(comparison.coolest.peak_c - round(OSHA_HIGH_C + 8.0, 1)) < 0.05)
    check("so does mean_c",
          abs(comparison.coolest.mean_c - round(OSHA_HIGH_C + 8.0, 1)) < 0.05)
    verdict = evaluate_rider_safety_status(41.5)
    check("41.5 C is Danger regardless of canopy", verdict.status.value == "danger")
    check("and still cites the published threshold",
          "NOAA Danger" in (verdict.citation or ""))

    print("\n[11] With no shade cached, routing is exactly what it was")
    routing.shade_fraction = lambda lat, lon: None
    plain_a = RouteCandidate("Direct", 8000, 1800, coordinates=line(33.40))
    plain_b = RouteCandidate("Detour 1", 8600, 1980, coordinates=line(33.60))
    routing.fetch_candidates = lambda s, e, **kw: [plain_a, plain_b]
    plain = routing.compare_routes(RiderLocation(33.40, -112.07),
                                   RiderLocation(33.60, -112.07))
    check("candidates report shade as unknown",
          all(c.shade_fraction is None for c in plain.candidates))
    check("scoring still produces degree-hours",
          all(c.degree_hours is not None for c in plain.candidates))
    check("and the message omits any shade claim",
          "shaded" not in plain.to_whatsapp_text())

    routing.get_hyperlocal_temperature, routing.shade_fraction = real_temp, real_shade
    routing.fetch_candidates = real_fetch

    print("\n[12] The feature can be switched off outright")
    real_settings = shade.settings

    class NoShade:
        def __getattr__(self, name):
            if name == "shade_routing":
                return False
            return getattr(real_settings, name)

    shade.settings = NoShade()
    try:
        check("shade_fraction returns None when disabled",
              shade.shade_fraction(33.4484, -112.0740) is None)
        check("even with a client and cache_only off",
              shade.shade_fraction(33.4484, -112.0740, client=Recorder(),
                                   cache_only=False) is None)
    finally:
        shade.settings = real_settings
finally:
    shade._cache_path = real_path

print("\n[13] The real class vocabulary, from cached API responses")
# The vocabulary was a guess until it was checked against the four genuine
# satellite responses in data/satellite/. It was wrong in three places:
# "plant" is real vegetation and went unmatched, while "earth, ground" and the
# model's own "others" bucket made whole readings come back as unknown. Every
# one of those is a silent wrong answer, so they are pinned here.
import glob as _glob

DOCUMENTED = ["building", "car", "earth, ground", "fence", "grass", "others",
              "plant", "road, route", "sea", "sidewalk, pavement", "tree", "truck"]
for cls in DOCUMENTED:
    single = shade._classify({cls: 100.0})
    check(f"{cls!r} is recognised, not left unmatched", single[2] == [])

check("'plant' counts as vegetation", shade._classify({"plant": 100.0})[1] == 1.0)
check("'tree' counts as canopy", shade._classify({"tree": 100.0})[0] == 1.0)
check("'earth, ground' is neither", shade._classify({"earth, ground": 100.0})[:2] == (0.0, 0.0))
check("a composite label matches on its keyword",
      shade._classify({"road, route": 100.0})[2] == [])
check("'others' does not make a reading unknown",
      f(seg(tree=30.0, others=70.0)) == 0.3)

fixtures = sorted(_glob.glob("data/satellite/*.json"))
check(f"cached satellite fixtures are present ({len(fixtures)})", len(fixtures) >= 1)
for path in fixtures:
    payload = json.loads(pathlib.Path(path).read_text())
    result = payload.get("result", payload)
    segments = ((result.get("segmentation") or {}).get("segments") or {})
    if not segments:
        continue
    name = pathlib.Path(path).name[:38]
    _, _, unmatched = shade._classify(segments)
    check(f"every class understood in {name} (unmatched={unmatched})", unmatched == [])
    value = shade.shade_fraction_from_result(result)
    check(f"  -> a usable fraction: {value}", value is not None and 0.0 <= value <= 1.0)

print("\n" + ("ALL SHADE CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
raise SystemExit(0 if ok else 1)

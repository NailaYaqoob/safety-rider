# Safety Rider — 3-minute demo script

**Hard cap: 3:00.** Judges stop watching at 3:00 whether you're done or not.
Target 2:50 so a slow cut doesn't push you over.

Judging weights this script is built against: **Impact & Relevance 40%,
Technical Execution 35%, Innovation 15%, Communication 10%.** The first 45
seconds carry the 40% — lead with the buyer's problem, not the architecture.

Narration is word-counted at ~150 wpm. Total 376 words ≈ 2:30 of speech,
leaving ~20s of silence for the reply landing and the slow scroll.

---

## Pre-flight — do these before you hit record

Each one is a way the demo dies on camera.

1. **Railway volume mounted at `/app/data`.** Without it the hourly cache is
   wiped on every redeploy and every reply falls back to yesterday's peak — the
   "right now" line never appears.
2. **Warm the Phoenix cell.** Easiest is to set
   `SAFETY_RIDER_WARM_CELLS=33.4484,-112.0740` on the deployment and let the
   built-in schedule do it — it warms at boot and then hourly, so a redeploy an
   hour before you record is enough. To force one by hand instead, on the box
   with the volume mounted:
   `python -m safety_rider.warm 33.4484 -112.0740`
   Either way it is one billed request and takes ~4 minutes. Do it the day you
   record, not the day before — the cache key is hour-scoped and expires on its
   own.
3. **`SAFETY_RIDER_DEV_TOOLS=1`** only while recording, **back to `0`** the
   moment you stop. A public URL with it on is a message relay anyone can fire.
4. **Confirm `SAFETY_RIDER_DEBUG_PAYLOADS=0`.** Payloads carry rider phone
   numbers and precise coordinates; they must not appear on screen.
5. **Blur or replace the phone number** in every WhatsApp frame.
6. **Dry-run the whole thing once, timed.** If a shot runs long it's Shot 4
   (routing) that gets trimmed, not the rider reply.
7. **Do not claim shade routing on camera.** The canopy scoring is in the
   code and tested, but `/v1/satellite` is Premium-only and this key is on the
   Hackathon plan, so no route in the demo is ranked by shade. Say "routes by
   measured street-level temperature" — which is exactly what a judge will see
   happen. If you want to mention shade at all, mention it as built and gated
   by tier; that reads far better than a claim a judge can probe.
8. **Coverage is U.S.-only.** Every coordinate on screen must be a U.S. city.
   Phoenix is FortyGuard's own example — use it.

**If inbound WhatsApp still isn't delivering** (Meta has never POSTed to the
webhook): record the rider side with `POST /api/dashboard/simulate` driving the
outbound message, and say plainly in narration that this is a simulated inbound.
Do not stage a fake screen recording of a message that never arrived.

---

## Shot 1 — The problem  (0:00–0:22)

**On screen:** a city-wide weather app showing one number for Phoenix, then cut
to the Safety Rider map with per-tile colours across the same metro.

> A courier fleet runs a hundred riders across Phoenix on schedules set by
> software that has no idea how hot the street is. Their weather app reports one
> number for the whole metro. But the gap between a shaded side street and the
> asphalt lot beside it runs several degrees — and that gap is where heat
> illness happens. One advisory is too alarming for half the city and not
> alarming enough for the other half.

*(76 words)*

---

## Shot 2 — The rider  (0:22–1:08)

**On screen:** phone, full frame. Share location in WhatsApp. Let the real
latency play — don't cut it. Then the reply lands.

> So a rider shares their location in WhatsApp. No app to install.

*(Pause — let the reply arrive on camera.)*

> Back comes a verdict for exactly where they're standing: how hot it is right
> now, how many hours a day that block stays above the OSHA high-heat line, and
> whether to keep riding. Every number is anchored to a published threshold and
> names the date it measured. Nothing here is a vibe — it's a decision the
> operator can defend.

*(72 words, including the one-liner above)*

**Zoom the reply** so the band, the temperature, and the duration line are all
legible on a phone screen. This is the shot the judges remember.

---

## Shot 3 — Dispatch  (1:08–1:38)

**On screen:** the live console. Riders framed on the map, event feed, trend
chart with its threshold lines.

> Dispatch sees the whole fleet at once. Every threshold crossing arrives as it
> fires, not when the drop is late. The trend chart draws its danger lines from
> the same constants the banding engine uses — so the picture can never disagree
> with the colour next to it.

*(48 words)*

---

## Shot 4 — Routing around the heat  (1:38–2:12)

**On screen:** the Phoenix candidate-route table, coolest option highlighted.

> Then the part that isn't just a warning. Given a pickup and a drop, we pull
> real road geometry from OSRM, sample the heat along each candidate, and rank
> them by exposure rather than by minutes. The coolest route is often not the
> fastest — and for a fleet, a rider who finishes the shift is worth more than
> one who saves four minutes.

*(64 words)*

---

## Shot 5 — What we measured  (2:12–2:42)

**On screen:** the README "What we measured" section, scrolling slowly. Land on
the `filter_type` discovery table.

> Building this taught us things the docs don't say. Ask for today's heatmap and
> the API returns success with every tile flat — an un-ingested day looks
> identical to a real one unless you check for it. Single-hour layers are flat
> by construction, so the same check has to be switched off on that path.
> Forecast hours come back well-formed and completely empty. We found each of
> these the expensive way, and every one is guarded in code and covered by
> tests — two hundred and thirty-seven of them.

*(89 words)*

---

## Shot 6 — Close  (2:42–2:55)

**On screen:** the "Who buys this, and why" table.

> The customer is the fleet, not the rider. The rider gets safety. The operator
> gets a heat decision it can defend to a regulator. That's who signs.

*(27 words)*

---

## Trimming order, if you run long

1. Shot 4 narration → cut to "we rank routes by exposure, not minutes" (one line).
2. Shot 5 → keep only the flat-tile trap and the test count.
3. Shot 1 → drop the last sentence.

Never trim Shot 2. The reply landing on a real phone is the entire pitch.

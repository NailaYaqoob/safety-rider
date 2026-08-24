# Deploying Safety Rider

The service needs a **persistent process**, not a serverless function. Railway,
Render, and Fly all work. Vercel does not — see [below](#why-not-vercel).

---

## Railway

### 1. Create the project

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Pick `NailaYaqoob/safety-rider`, branch `main`

Railway reads [`railway.toml`](railway.toml) and builds from the
[`Dockerfile`](Dockerfile). No build command to configure.

### 2. Set the environment variables

**Variables** tab → **Raw Editor** → paste, filling in your own values:

```
FORTYGUARD_API_KEY=your-key
WHATSAPP_VERIFY_TOKEN=your-verify-token
WHATSAPP_APP_SECRET=your-app-secret
WHATSAPP_ACCESS_TOKEN=your-system-user-token
WHATSAPP_PHONE_NUMBER_ID=your-phone-number-id
WHATSAPP_GRAPH_API_VERSION=v21.0

SAFETY_RIDER_LIVE_HEAT=1
SAFETY_RIDER_DAYS_BACK=2
SAFETY_RIDER_BACKFILL_DAYS=6
SAFETY_RIDER_NOWCAST=1            # use warmed current-hour layers; 0 = daily only
SAFETY_RIDER_NOWCAST_LOOKBACK=3   # hours to step back if now is not warm (cache reads, free)
#
# The nowcast is never fetched on a rider's message — an hourly layer takes
# ~4 minutes. Warm it out of band instead, one run per hour per service area:
#   python -m safety_rider.warm <lat> <lon>
SAFETY_RIDER_HEAT_TIMEOUT_S=120
SAFETY_RIDER_DEBUG_PAYLOADS=0

SAFETY_RIDER_DEV_TOOLS=0
SAFETY_RIDER_DASHBOARD_UNMASK=0
```

> **Never commit these.** `.env` is git-ignored and `.dockerignore` keeps it out
> of the image; Railway injects them at runtime.

Two of those defaults are deliberately different from local development:

| Variable | Local | Deployed | Why |
|---|---|---|---|
| `SAFETY_RIDER_DEV_TOOLS` | `1` | **`0`** | Disables `/api/dashboard/simulate`, which sends a real WhatsApp message. A public URL with it enabled is a message relay anyone can trigger. |
| `SAFETY_RIDER_DASHBOARD_UNMASK` | `0` | `0` | Keeps rider phone numbers masked. |

**For the demo video**, set `SAFETY_RIDER_DEV_TOOLS=1` and
`SAFETY_RIDER_MOCK_TEMPERATURE=1` so the Simulate button works and costs no
credits. Turn dev tools back off afterwards.

### 3. Generate the public URL

**Settings** → **Networking** → **Generate Domain**. You get a permanent
`*.up.railway.app` host. Nothing else needs configuring — the container reads
`$PORT` from Railway.

### 4. Point Meta at it

App Dashboard → WhatsApp → Configuration:

- **Callback URL:** `https://<your-app>.up.railway.app/webhook/whatsapp`
- **Verify token:** your `WHATSAPP_VERIFY_TOKEN`
- Save, then **subscribe to the `messages` field** (separate step)

This URL is stable, so unlike a quick tunnel you only paste it once.

### 5. Add a volume (recommended)

**Settings** → **Volumes** → mount at **`/app/data`**.

Without it, every redeploy starts with an empty heat-layer cache and the next
rider in each area re-bills the FortyGuard API — roughly **8,440 credits per
grid cell**. The volume makes the cache survive deploys.

### Verify

```bash
curl https://<your-app>.up.railway.app/health
```

Expect `{"status":"ok","configured":{...all true...}}`. Then open
`https://<your-app>.up.railway.app/dashboard`.

---

## Why not Vercel

Vercel runs serverless functions. Four things in this service need a process
that stays alive between requests:

| Feature | What Vercel does |
|---|---|
| **SSE alert feed** (`/api/dashboard/stream`) | Functions have a hard execution cap. The feed dies mid-stream and reconnects in a loop. |
| **Event hub + rider registry** (in-memory) | Each invocation may be a fresh instance. Riders vanish from the map between requests. |
| **Background tasks after the 200** | Work queued after the response is killed when the function returns — so the heat lookup and the WhatsApp reply never run. |
| **Blocking FortyGuard polls** (up to 120 s) | Exceeds the function timeout on every tier below Enterprise. |
| **Heat-layer disk cache** | Filesystem is read-only apart from ephemeral `/tmp`. Every request re-bills the API. |

These are not configuration problems; they are what serverless is. Keep Vercel
for a static marketing page if you want one, and run the service on Railway.

---

## Other platforms

The `Dockerfile` is plain and portable:

- **Render** — New Web Service → Docker → health check path `/health`
- **Fly.io** — `fly launch --dockerfile Dockerfile` then `fly deploy`

Both need the same environment variables and a volume at `/app/data`.

---

## One replica only

The Dockerfile pins `--workers 1` and `railway.toml` sets `numReplicas = 1`.

The event hub and the `wamid` deduplication cache are both in-memory. With two
workers a rider handled by one would never appear on a dashboard streamed from
the other, and a Meta retry could be answered twice. Both need to move to Redis
before scaling out — until then, one replica is a correctness requirement, not
a cost saving.

"""Environment configuration for the Safety Rider service.

Everything the service needs comes from environment variables, loaded from the
repo-root ``.env`` by ``python-dotenv`` (the same file the notebooks already use
for ``FORTYGUARD_API_KEY``).

Nothing here raises at import time — a missing variable should surface as a clear
error on the request that actually needs it, not as a crash on ``import``. Call
:func:`validate_startup` from the app factory to fail fast on a misconfigured
deployment instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Repo root == the directory containing this package.
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, treating whitespace-only values as unset."""
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Instantiate once via :data:`settings`."""

    # ── WhatsApp / Meta ────────────────────────────────────────────────────
    #: Arbitrary string you invent and paste into the Meta App Dashboard when
    #: subscribing the webhook. Meta echoes it back on the GET verification
    #: handshake; we compare and only then return the challenge.
    verify_token: str | None = field(
        default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN")
    )

    #: The Meta *App Secret* (App Dashboard → Settings → Basic). Used to verify
    #: the ``X-Hub-Signature-256`` header on every POST so we only act on
    #: payloads Meta actually signed. Without it anyone who learns the URL can
    #: forge messages.
    app_secret: str | None = field(
        default_factory=lambda: _env("WHATSAPP_APP_SECRET")
    )

    #: Permanent (or system-user) access token used as the Graph API bearer.
    access_token: str | None = field(
        default_factory=lambda: _env("WHATSAPP_ACCESS_TOKEN")
    )

    #: Numeric ID of the sending phone number (WhatsApp Manager → API Setup).
    #: This is NOT the phone number itself.
    phone_number_id: str | None = field(
        default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID")
    )

    #: Graph API version. Meta deprecates versions on a ~2-year cadence, so pin
    #: it explicitly rather than tracking whatever is current.
    graph_api_version: str = field(
        default_factory=lambda: _env("WHATSAPP_GRAPH_API_VERSION", "v21.0") or "v21.0"
    )

    # ── FortyGuard ─────────────────────────────────────────────────────────
    #: Already used by the notebooks; re-read here so the service can report a
    #: missing key at startup rather than on the first rider message.
    fortyguard_api_key: str | None = field(
        default_factory=lambda: _env("FORTYGUARD_API_KEY")
    )

    # ── Heat layer ─────────────────────────────────────────────────────────
    #: Set falsy to force the stub evaluator even when a FortyGuard key exists.
    #: Useful for demos and for testing the WhatsApp path without spending credits.
    heat_live: bool = field(
        default_factory=lambda: (_env("SAFETY_RIDER_LIVE_HEAT") or "1").lower()
        not in {"0", "false", "no"}
    )

    #: Cache-grid cell size in degrees (~0.05° ≈ 5.5 km). Riders inside one cell
    #: share a single fetched layer. Must stay well below the AOI size, or a
    #: rider near a cell edge falls outside the layer fetched for its centre.
    heat_grid_deg: float = field(
        default_factory=lambda: float(_env("SAFETY_RIDER_GRID_DEG") or 0.05)
    )

    #: Half the AOI square's side, in metres. 5000 m → a 10 × 10 km AOI
    #: (~100 km²), comfortably covering a 5.5 km grid cell plus a margin.
    heat_aoi_half_side_m: float = field(
        default_factory=lambda: float(_env("SAFETY_RIDER_AOI_HALF_SIDE_M") or 5000)
    )

    #: Heatmap spatial resolution. The API accepts 60, 80, or 100 metres only.
    #: 100 m over a 100 km² AOI is ~10,000 tiles — finer costs time, not accuracy,
    #: at the scale a rider cares about.
    heat_granularity_m: int = field(
        default_factory=lambda: int(_env("SAFETY_RIDER_GRANULARITY_M") or 100)
    )

    #: How many days back to measure. **Must not be 0.**
    #:
    #: The catalog covers 2021→today, but "today" means *the hours that have
    #: already elapsed*, not the whole day. Verified on 2026-08-20: querying
    #: today mid-morning returned an 18.8 °C peak and 0.0 exceedance hours on
    #: every tile, while a complete August day at the same location peaks at
    #: 36–37 °C. A rider asking at 08:00 would be told "clear to ride" on a day
    #: that goes on to hit 36 °C — so we measure the last COMPLETE day instead.
    #: Two, not one. Measured 2026-08-23: yesterday's layer was still being
    #: ingested and came back as a single hour, while the day before was
    #: complete. Starting at 1 costs a wasted heatmap request before the
    #: partial-day check steps back, and heatmaps are billed per request.
    heat_days_back: int = field(
        default_factory=lambda: max(1, int(_env("SAFETY_RIDER_DAYS_BACK") or 2))
    )

    #: Fetch the current hour's layer alongside the daily one, so the rider is
    #: told what it is *now* rather than only what the last complete day peaked
    #: at. Measured 2026-08-24: the current hour returns real data; six hours
    #: ahead returns an empty layer, so this is a nowcast, never a forecast.
    #:
    #: Costs nothing on the rider path: the hourly layer is read cache-only and
    #: a cold cell simply has no nowcast. The spend happens in
    #: :mod:`safety_rider.warm`, out of band, at one request per (cell, hour) —
    #: bounded by service area rather than by traffic. Set
    #: SAFETY_RIDER_NOWCAST=0 to ignore the hourly layer entirely.
    nowcast: bool = field(
        default_factory=lambda: (_env("SAFETY_RIDER_NOWCAST") or "1").lower()
        not in {"0", "false", "no"}
    )

    #: How many hours to step back when the current hour is not warm. These are
    #: cache lookups, not requests, so this is free — it exists so a cell warmed
    #: at :00 still answers at :59.
    nowcast_lookback_hours: int = field(
        default_factory=lambda: max(1, int(_env("SAFETY_RIDER_NOWCAST_LOOKBACK") or 3))
    )

    #: How many further days back to try when a day comes back partial.
    #: FortyGuard ingests a day some time after it ends, and an un-ingested day
    #: does not error — it returns one hour's snapshot with min == mean == max,
    #: which reads as a cool, safe day and is the single most dangerous wrong
    #: answer this service can give. Measured 2026-08-23: 1 day back was still
    #: partial, 7 days back was complete. Each step costs one heatmap request,
    #: so keep this small.
    heat_backfill_days: int = field(
        default_factory=lambda: max(1, int(_env("SAFETY_RIDER_BACKFILL_DAYS") or 6))
    )

    #: Poll budget in seconds for one FortyGuard task. The client defaults to
    #: 600 s, which is meaningless to someone waiting on WhatsApp — cap it so a
    #: slow task fails fast and the rider gets the fallback answer instead.
    heat_timeout_s: float = field(
        default_factory=lambda: float(_env("SAFETY_RIDER_HEAT_TIMEOUT_S") or 120)
    )

    #: Poll budget for the out-of-band warmer, which is under no such pressure.
    #: It has to be generous: the same request measured 219 s on 2026-08-24 at
    #: 12:47 UTC and was still 'Processing' past 120 s at 15:43 the same day, so
    #: queue depth — not tile count — sets the wait. Timing out here is the
    #: expensive failure, because abandoning the poll does not cancel the billed
    #: job; it only throws away the answer you already paid for.
    heat_warm_timeout_s: float = field(
        default_factory=lambda: float(_env("SAFETY_RIDER_HEAT_WARM_TIMEOUT_S") or 900)
    )

    #: Set truthy to answer every temperature lookup from the deterministic
    #: simulator in :mod:`safety_rider.temperature_service` instead of the API.
    #: Distinct from SAFETY_RIDER_LIVE_HEAT, which disables the whole live path;
    #: this one is the demo switch — no key, no credits, no network, and the
    #: same pin always returns the same number.
    mock_temperature: bool = field(
        default_factory=lambda: (_env("SAFETY_RIDER_MOCK_TEMPERATURE") or "").lower()
        in {"1", "true", "yes"}
    )

    #: Pin the simulated temperature to an exact °C value. The only sane way to
    #: demo the Danger branch on a mild day, and what the tests use to hit an
    #: exact band boundary. Unset = derive it from the location.
    mock_temp_c: float | None = field(
        default_factory=lambda: (
            float(_env("SAFETY_RIDER_MOCK_TEMP_C"))
            if _env("SAFETY_RIDER_MOCK_TEMP_C") is not None
            else None
        )
    )

    #: Set truthy to ignore cached layers and re-bill every call.
    heat_refresh: bool = field(
        default_factory=lambda: (_env("SAFETY_RIDER_HEAT_REFRESH") or "").lower()
        in {"1", "true", "yes"}
    )

    # ── Routing (safety_rider/routing.py) ──────────────────────────────────
    #: OSRM instance used for candidate routes. The public demo works but is
    #: rate-limited, returns no alternatives, and offers no uptime guarantee —
    #: run your own before anyone depends on this.
    osrm_base_url: str = field(
        default_factory=lambda: _env("SAFETY_RIDER_OSRM_URL")
        or "https://router.project-osrm.org"
    )

    #: OSRM profile. The public demo only serves "driving"; a self-hosted
    #: instance can offer "cycling", which matches a bike courier better.
    osrm_profile: str = field(
        default_factory=lambda: _env("SAFETY_RIDER_OSRM_PROFILE") or "driving"
    )

    osrm_timeout_s: float = field(
        default_factory=lambda: float(_env("SAFETY_RIDER_OSRM_TIMEOUT_S") or 15)
    )

    #: Hard cap on heat-grid cells sampled per route. Each NEW cell costs two
    #: heatmap requests (~8,440 credits), so without this a long route could
    #: quietly spend a fortune. Cells already cached are free.
    max_route_cells: int = field(
        default_factory=lambda: max(1, int(_env("SAFETY_RIDER_MAX_ROUTE_CELLS") or 4))
    )

    # ── Dashboard / demo ───────────────────────────────────────────────────
    #: Set truthy to show riders' full phone numbers on the dashboard. Off by
    #: default because the dashboard is meant to be screen-shared.
    dashboard_unmask: bool = field(
        default_factory=lambda: (_env("SAFETY_RIDER_DASHBOARD_UNMASK") or "").lower()
        in {"1", "true", "yes"}
    )

    #: Enables the "Simulate Heat Spike" endpoint. It sends a REAL WhatsApp
    #: message, so it is a live capability, not a stub — turn it off outside
    #: demos.
    dev_tools: bool = field(
        default_factory=lambda: (_env("SAFETY_RIDER_DEV_TOOLS") or "1").lower()
        not in {"0", "false", "no"}
    )

    #: The only number the simulate endpoint will message. Deliberately taken
    #: from the environment rather than the request body: the dashboard sits
    #: behind a public tunnel during a demo, and an endpoint that messages an
    #: arbitrary number on request is a spam relay waiting to be found.
    demo_number: str | None = field(
        default_factory=lambda: _env("SAFETY_RIDER_DEMO_NUMBER")
    )

    #: Where the dashboard map opens. Defaults to San Jose — inside FortyGuard
    #: coverage, and the area every cached fixture in this repo describes.
    map_center_lat: float = field(
        default_factory=lambda: float(_env("SAFETY_RIDER_MAP_LAT") or 37.3318)
    )
    map_center_lon: float = field(
        default_factory=lambda: float(_env("SAFETY_RIDER_MAP_LON") or -121.8899)
    )

    # ── Service behaviour ──────────────────────────────────────────────────
    #: Set truthy to log full inbound payloads. Leave off in production — the
    #: payloads contain rider phone numbers and precise locations.
    debug_payloads: bool = field(
        default_factory=lambda: (_env("SAFETY_RIDER_DEBUG_PAYLOADS") or "").lower()
        in {"1", "true", "yes"}
    )

    @property
    def graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    @property
    def messages_url(self) -> str:
        """Graph endpoint that sends a message from our business number."""
        return f"{self.graph_base_url}/{self.phone_number_id}/messages"


settings = Settings()


class ConfigurationError(RuntimeError):
    """A required environment variable is missing or empty."""


def validate_startup(*, require_outbound: bool = True) -> None:
    """Fail fast on a misconfigured deployment.

    ``require_outbound=False`` allows running verification-only (useful while
    you are still getting Meta to accept the callback URL and have not yet
    issued an access token).
    """
    missing: list[str] = []
    if not settings.verify_token:
        missing.append("WHATSAPP_VERIFY_TOKEN")
    if not settings.app_secret:
        missing.append("WHATSAPP_APP_SECRET")
    if require_outbound:
        if not settings.access_token:
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not settings.phone_number_id:
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
    if missing:
        raise ConfigurationError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

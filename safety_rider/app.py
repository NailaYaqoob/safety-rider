"""FastAPI application for the Safety Rider service.

Run locally::

    pip install -r requirements-service.txt
    uvicorn safety_rider.app:app --reload --port 8000

Then expose it to Meta — the Cloud API only calls **public HTTPS** URLs, so a
localhost port will not do::

    ngrok http 8000

Paste ``https://<your-ngrok-subdomain>.ngrok-free.app/webhook/whatsapp`` into the
Meta App Dashboard as the Callback URL, with ``WHATSAPP_VERIFY_TOKEN`` as the
Verify Token, then subscribe to the **messages** field.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigurationError, settings, validate_startup
from .dashboard import router as dashboard_router
from .dashboard.routes import STATIC as DASHBOARD_STATIC
from .events import hub
from .warm import run_scheduler, scheduler_should_run
from .whatsapp import router as whatsapp_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(
    title="Safety Rider",
    description="Heat-exposure intelligence for riders, delivered over WhatsApp.",
    version="0.1.0",
)

app.include_router(whatsapp_router)
app.include_router(dashboard_router)
# Leaflet is vendored rather than pulled from a CDN: a pitch demo must not be
# able to fail because someone else's network did.
app.mount("/dashboard/static", StaticFiles(directory=DASHBOARD_STATIC), name="dashboard-static")


@app.on_event("startup")
async def _startup() -> None:
    """Warn loudly about missing configuration without refusing to boot.

    Booting anyway is deliberate: during setup you often have the verify token
    before you have an access token, and you need the service reachable to
    complete Meta's handshake at all.
    """
    try:
        validate_startup()
        log.info("Configuration OK — outbound messaging enabled.")
    except ConfigurationError as exc:
        log.warning("%s", exc)
        log.warning(
            "Service is running in degraded mode: replies will be logged, not sent."
        )
    if not settings.fortyguard_api_key:
        log.warning("FORTYGUARD_API_KEY is not set — heat risk will use stub data.")
    restored = hub.load()
    if restored:
        log.info(
            "Restored %d rider(s) from the last run — a routing request can be "
            "answered without asking them to re-share their location.",
            restored,
        )
    if settings.dev_tools:
        log.warning(
            "Dev tools ENABLED: POST /api/dashboard/simulate sends a real "
            "WhatsApp message to SAFETY_RIDER_DEMO_NUMBER. Set "
            "SAFETY_RIDER_DEV_TOOLS=0 to disable."
        )
    should_warm, why = scheduler_should_run()
    if should_warm:
        # Held on the app so the shutdown hook can cancel it. Without that the
        # loop closes with a task still polling FortyGuard and asyncio prints a
        # "Task was destroyed but it is pending" traceback over the shutdown.
        app.state.warm_task = asyncio.create_task(run_scheduler())
        log.info("Nowcast warmer started (%s).", why)
    else:
        app.state.warm_task = None
        log.info("Nowcast warmer not running: %s. Riders fall back to the last "
                 "complete day, which is a correct answer with an older "
                 "timestamp.", why)

    log.info("Dashboard: http://127.0.0.1:8000/dashboard")


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Stop the warmer before the loop closes."""
    task = getattr(app.state, "warm_task", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send the bare domain to the dashboard.

    The deployed URL is what a reviewer pastes into a browser, and an app with
    no root route answers that with a bare ``{"detail":"Not Found"}``. The
    dashboard is the only page a human wants here, so point at it.
    """
    return RedirectResponse(url="/dashboard")


@app.get("/health", tags=["ops"])
async def health() -> dict[str, object]:
    """Liveness probe, plus an at-a-glance view of what is configured.

    Reports only booleans — never echo secrets from an unauthenticated endpoint.
    """
    return {
        "status": "ok",
        "configured": {
            "verify_token": bool(settings.verify_token),
            "app_secret": bool(settings.app_secret),
            "access_token": bool(settings.access_token),
            "phone_number_id": bool(settings.phone_number_id),
            "fortyguard_api_key": bool(settings.fortyguard_api_key),
        },
        "graph_api_version": settings.graph_api_version,
    }

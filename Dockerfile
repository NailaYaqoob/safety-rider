# Safety Rider — single-process FastAPI service.
#
# ONE worker, deliberately. The event hub (safety_rider/events.py) and the
# wamid dedup cache (whatsapp/webhook.py) are both in-memory, so a second
# worker would silently split riders between them: a rider handled by worker B
# would never appear on a dashboard streaming from worker A, and a Meta retry
# could be answered twice. Move both to Redis before scaling out.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change does not re-install them.
COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY fortyguard/ ./fortyguard/
COPY safety_rider/ ./safety_rider/

# Cache directory for fetched heat layers. Mount a volume here in production —
# on an ephemeral filesystem every redeploy re-bills the FortyGuard API
# (~8,440 credits per grid cell).
RUN mkdir -p /app/data/heatmaps /app/data/env_params

EXPOSE 8000

# $PORT is injected by Railway/Render/Fly; 8000 is the local fallback. Shell
# form so the variable is expanded at runtime rather than baked in at build.
CMD uvicorn safety_rider.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1

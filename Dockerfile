# =============================================================================
# XIOSYNC — Production Dockerfile
# =============================================================================
# Normative: doc 09 §8 (INV-CONTAINER-1), D-013, D-016.
#
# Multi-stage build: builder → base → api | migration-job | worker
#
# INV-CONTAINER-1: No secrets, no .vault_key, no secrets.json baked in.
#   Secrets are injected from the managed backend at runtime via env vars.
#
# Three named targets:
#   api           — the FastAPI control-plane (long-running)
#   migration-job — discrete deploy step: runs `alembic upgrade head` then exits
#   worker        — execution-plane worker (placeholder; Phase 4+ detail)
#
# Build examples:
#   docker build --target api           -t xiosync-api:latest .
#   docker build --target migration-job -t xiosync-migration:latest .
#   docker build --target worker        -t xiosync-worker:latest .
#
# Required runtime env vars (api + migration-job):
#   DATABASE_URL         postgresql+psycopg://...  (INV-CFG-1, C6)
#   XIOSYNC_ENVIRONMENT  dev | ci | staging | production
#   XIOSYNC_AUTH_SECRET  >=32 chars random string  (L4 — no defaults)
# Optional:
#   REDIS_URL            redis://...               (rate limiting — M1)
#   CORS_ALLOWED_ORIGINS https://app.example.com   (INV-CORS-1, C4)
#   XIOSYNC_LOG_LEVEL    INFO | DEBUG | ...
# =============================================================================

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV UV_VERSION=0.9.22 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:/root/.local/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY xiosync/ ./xiosync/
COPY alembic.ini ./


# ── Stage 2: runtime base ─────────────────────────────────────────────────────
FROM python:3.13-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv     /app/.venv
COPY --from=builder /app/xiosync   /app/xiosync
COPY --from=builder /app/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --shell /bin/bash --uid 1001 xiosync \
    && chown -R xiosync:xiosync /app

USER xiosync

LABEL org.opencontainers.image.source="https://github.com/MDShahid94/XIOSYNC_V0" \
      org.opencontainers.image.description="XIOSYNC Control Plane (v1)" \
      org.opencontainers.image.licenses="UNLICENSED"


# ── Target: api ───────────────────────────────────────────────────────────────
FROM base AS api

EXPOSE 8000

# Liveness probe — /live always 200; /ready checks migration head (INV-HEALTH-1)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD curl -f http://localhost:8000/live || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "xiosync.api.app:app", "--host", "0.0.0.0", "--port", "8000"]


# ── Target: migration-job ─────────────────────────────────────────────────────
FROM base AS migration-job

# One-shot deploy step — run BEFORE rolling out API replicas (INV-DEPLOY-1, C6).
# Exit 0 on success, non-zero on failure. Release pipeline must gate API
# rollout on this exit code.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "alembic", "upgrade", "head"]


# ── Target: worker ────────────────────────────────────────────────────────────
FROM base AS worker

# Execution-plane worker — receives tasks via lease API (doc 07 §3, D-007).
# Worker credentials: short-lived, per-worker, capability-scoped (H7 fix).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "xiosync.worker.main"]

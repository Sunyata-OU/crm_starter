# syntax=docker/dockerfile:1

# Two stages: dependencies are resolved once and copied into a runtime image
# that carries no build tools. The result is smaller, and rebuilds after a code
# change reuse the dependency layer.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, and *only* the files that describe them. A change to the
# application code then leaves this layer cached, which is the difference
# between a two-second rebuild and a two-minute one.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM python:3.13-slim-bookworm AS runtime

# curl for the healthcheck; nothing else is needed at runtime.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# A non-root user, and the numeric id is fixed so a mounted volume's ownership
# is predictable on the host.
RUN groupadd --gid 1000 crm \
    && useradd --uid 1000 --gid crm --create-home crm

WORKDIR /app

COPY --from=builder --chown=crm:crm /app /app

# Uploaded files live on a volume; without one they vanish with the container.
RUN mkdir -p /app/var/files && chown -R crm:crm /app/var

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CRM_ENVIRONMENT=production \
    CRM_FILE_ROOT=/app/var/files

USER crm

EXPOSE 8000

# Hits the readiness endpoint, which checks the connections rather than merely
# proving the process is alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

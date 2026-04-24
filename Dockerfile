# =============================================================================
# Obscura SDK — slim image (core + server + telemetry only).
#
# For a "batteries included" image that bakes in every plugin pip dep plus the
# binary tools non-pip plugins shell out to (jq, rg, kubectl, playwright, …),
# build `Dockerfile.full` instead: `docker build -f Dockerfile.full -t obscura:full .`
# =============================================================================

# Stage 1: Builder — install deps with uv, compile the venv
FROM python:3.13.5-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN pip install --no-cache-dir uv

# Manifests only — keeps dep-install cached when only source changes.
COPY pyproject.toml uv.lock ./

# Install third-party deps only (no project source yet).
RUN uv sync --frozen --no-dev --no-install-project \
        --extra server --extra telemetry

COPY obscura/ obscura/
COPY scripts/ scripts/

# Install the project itself (fast — deps already cached).
RUN uv sync --frozen --no-dev --extra server --extra telemetry

# Stage 2: Runtime — minimal image with only the venv + app code
FROM python:3.13.5-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends gh ca-certificates nodejs npm libpq-dev \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r obscura && useradd -r -g obscura -d /home/obscura -m obscura

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/obscura obscura/
COPY --from=builder /app/scripts scripts/

# Some packaged provider binaries lose executable mode in copied venv layers.
# Ensure Copilot CLI shim is runnable at runtime.
RUN find /app/.venv/lib -path "*/site-packages/copilot/bin/copilot" -type f -exec chmod 755 {} \;

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# Pin OBSCURA_HOME so paths.py resolves the .obscura directory correctly
# inside containers. The docker-compose volume bind maps
# ${HOME}/.obscura -> /home/obscura/.obscura, matching this path exactly.
ENV OBSCURA_HOME=/home/obscura/.obscura

USER obscura
EXPOSE 8080 50051

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

ENTRYPOINT ["python", "-m", "uvicorn", "obscura.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

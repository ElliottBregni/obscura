# =============================================================================
# Obscura SDK — Multi-stage Docker build
# =============================================================================

# Stage 1: Builder — install deps with uv, compile the venv
FROM python:3.13.5-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra server --extra telemetry

COPY obscura/ obscura/
COPY scripts/ scripts/

# Stage 2: Runtime — minimal image with only the venv + app code
FROM python:3.13.5-slim AS runtime

ARG INSTALL_GWS=0
ARG INSTALL_M365=0
ARG INSTALL_HF=0
RUN apt-get update \
    && apt-get install -y --no-install-recommends gh ca-certificates nodejs npm libpq-dev \
    && npm install -g @anthropic-ai/claude-code \
    && if [ "$INSTALL_GWS" = "1" ]; then npm install -g @googleworkspace/cli || true; fi \
    && if [ "$INSTALL_M365" = "1" ]; then npm install -g @pnp/cli-microsoft365 || true; fi \
    && if [ "$INSTALL_HF" = "1" ]; then pip install --no-cache-dir huggingface-hub || true; fi \
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

USER obscura
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

ENTRYPOINT ["python", "-m", "uvicorn", "obscura.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

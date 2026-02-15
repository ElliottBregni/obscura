# =============================================================================
# Obscura SDK — Multi-stage Docker build
# =============================================================================

# Stage 1: Builder — install deps with uv, compile the venv
FROM python:3.13.5-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra server --extra telemetry

COPY sdk/ sdk/
COPY scripts/ scripts/

# Stage 2: Runtime — minimal image with only the venv + app code
FROM python:3.13.5-slim AS runtime

RUN groupadd -r obscura && useradd -r -g obscura -d /home/obscura -m obscura

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/sdk sdk/
COPY --from=builder /app/scripts scripts/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

USER obscura
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

ENTRYPOINT ["python", "-m", "uvicorn", "sdk.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

"""
sdk.server -- FastAPI HTTP API wrapping the ObscuraClient SDK.

Endpoints
---------
POST /api/v1/send           Send a prompt and get a full response.
POST /api/v1/stream         Send a prompt and receive an SSE stream.
POST /api/v1/sessions       Create a new session.
GET  /api/v1/sessions       List sessions.
DELETE /api/v1/sessions/{id} Delete a session.
POST /api/v1/sync           Trigger vault sync.
GET  /health                Liveness probe (unauthenticated).
GET  /ready                 Readiness probe (unauthenticated).

Start the server via::

    obscura-sdk serve [--host 0.0.0.0] [--port 8080] [--reload]
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from sdk._types import Backend, ChunkKind, SessionRef
from sdk.auth.middleware import JWKSCache, JWTAuthMiddleware
from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import get_current_user, require_any_role, require_role
from sdk.client import ObscuraClient
from sdk.config import ObscuraConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global agent runtime registry (keyed by user_id)
# ---------------------------------------------------------------------------

_runtimes: dict[str, "AgentRuntime"] = {}
_runtimes_lock = asyncio.Lock()


async def _get_runtime(user: AuthenticatedUser) -> "AgentRuntime":
    """Get or create a persistent AgentRuntime for the given user."""
    from sdk.agents import AgentRuntime

    async with _runtimes_lock:
        if user.user_id not in _runtimes:
            runtime = AgentRuntime(user)
            await runtime.start()
            _runtimes[user.user_id] = runtime
        return _runtimes[user.user_id]


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def _audit(
    event_type: str,
    user: AuthenticatedUser,
    resource: str,
    action: str,
    outcome: str,
    **details: Any,
) -> None:
    """Emit an audit event (best-effort, never raises)."""
    try:
        from sdk.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(AuditEvent(
            event_type=event_type,
            user_id=user.user_id,
            user_email=user.email,
            resource=resource,
            action=action,
            outcome=outcome,
            details=details,
        ))
    except Exception:
        pass


def _record_sync_metric(status: str) -> None:
    """Record a sync_operations_total metric (best-effort)."""
    try:
        from sdk.telemetry.metrics import get_metrics
        m = get_metrics()
        m.sync_operations_total.add(1, {"status": status})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------

class SendRequest(BaseModel):
    """Request body for POST /api/v1/send."""
    backend: str = Field(default="copilot", description="Backend: 'copilot' or 'claude'")
    prompt: str = Field(..., min_length=1, description="User prompt text")
    model: str | None = Field(default=None, description="Raw model ID")
    model_alias: str | None = Field(default=None, description="copilot_models alias")
    system_prompt: str = Field(default="", description="System prompt")
    session_id: str | None = Field(default=None, description="Resume session by ID")


class SendResponse(BaseModel):
    """Response body for POST /api/v1/send."""
    text: str
    backend: str
    session_id: str | None = None


class StreamRequest(BaseModel):
    """Request body for POST /api/v1/stream."""
    backend: str = Field(default="copilot", description="Backend: 'copilot' or 'claude'")
    prompt: str = Field(..., min_length=1, description="User prompt text")
    model: str | None = Field(default=None, description="Raw model ID")
    model_alias: str | None = Field(default=None, description="copilot_models alias")
    system_prompt: str = Field(default="", description="System prompt")
    session_id: str | None = Field(default=None, description="Resume session by ID")


class SessionCreateRequest(BaseModel):
    """Request body for POST /api/v1/sessions."""
    backend: str = Field(default="copilot", description="Backend: 'copilot' or 'claude'")


class SessionResponse(BaseModel):
    """A single session reference."""
    session_id: str
    backend: str


class SyncRequest(BaseModel):
    """Request body for POST /api/v1/sync."""
    agent: str | None = Field(default=None, description="Specific agent to sync")
    repo: str | None = Field(default=None, description="Specific repo name or path")
    dry_run: bool = Field(default=False, description="Preview without changes")


class SyncResponse(BaseModel):
    """Response body for POST /api/v1/sync."""
    success: bool
    message: str


class HealthResponse(BaseModel):
    """Response body for GET /health and /ready."""
    status: str


# ---------------------------------------------------------------------------
# Client pool / factory
# ---------------------------------------------------------------------------

class ClientFactory:
    """Creates and manages per-request ObscuraClient instances.

    In production you would cache long-lived clients keyed by
    (backend, user_id).  For now each request gets a fresh client.
    """

    def __init__(self, config: ObscuraConfig) -> None:
        self._config = config

    async def create(
        self,
        backend: str,
        *,
        user: AuthenticatedUser | None = None,
        model: str | None = None,
        model_alias: str | None = None,
        system_prompt: str = "",
    ) -> ObscuraClient:
        client = ObscuraClient(
            backend,
            model=model,
            model_alias=model_alias,
            system_prompt=system_prompt,
            user=user,
        )
        await client.start()
        return client


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle for the server."""
    config: ObscuraConfig = app.state.config
    logger.info("Obscura SDK server starting (host=%s port=%d)", config.host, config.port)

    # Initialize telemetry (traces, metrics, structured logging)
    try:
        from sdk.telemetry import init_telemetry
        init_telemetry(config)
        logger.info("Telemetry initialized (otel_enabled=%s)", config.otel_enabled)
    except Exception:
        logger.warning("Could not initialize telemetry; continuing without observability")

    # Warm the JWKS cache
    if config.auth_enabled:
        jwks: JWKSCache = app.state.jwks_cache
        try:
            await jwks.refresh()
            logger.info("JWKS cache warmed (%d keys)", len(jwks.keys))
        except Exception:
            logger.warning("Could not pre-fetch JWKS; will retry on first request")

    yield

    logger.info("Obscura SDK server shutting down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(config: ObscuraConfig | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    Call with no arguments to load config from environment variables.
    """
    if config is None:
        config = ObscuraConfig.from_env()

    app = FastAPI(
        title="Obscura SDK API",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Stash shared state
    app.state.config = config
    app.state.client_factory = ClientFactory(config)

    # Telemetry middleware (must be added before auth so it wraps auth)
    if config.otel_enabled:
        try:
            from sdk.telemetry.middleware import ObscuraTelemetryMiddleware
            app.add_middleware(ObscuraTelemetryMiddleware)
        except ImportError:
            logger.debug("Telemetry middleware not available; skipping")

    # Auth middleware
    if config.auth_enabled:
        jwks_cache = JWKSCache(config.auth_jwks_uri)
        app.state.jwks_cache = jwks_cache
        app.add_middleware(
            JWTAuthMiddleware,
            jwks_cache=jwks_cache,
            issuer=config.auth_issuer,
            audience=config.auth_audience,
        )

    # CORS middleware
    import os
    cors_origins = os.environ.get("OBSCURA_CORS_ORIGINS", "http://localhost:*").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- routes ---------------------------------------------------------

    @app.get("/health", response_model=HealthResponse, tags=["infra"])
    async def health() -> HealthResponse:
        """Liveness probe -- always returns 200."""
        return HealthResponse(status="ok")

    @app.get("/ready", response_model=HealthResponse, tags=["infra"])
    async def ready() -> HealthResponse:
        """Readiness probe -- returns 200 when the server can serve traffic."""
        return HealthResponse(status="ok")

    # -- send -------------------------------------------------------------

    @app.post("/api/v1/send", response_model=SendResponse, tags=["agent"])
    async def send(
        body: SendRequest,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> SendResponse:
        """Send a prompt and receive the full response."""
        factory: ClientFactory = app.state.client_factory
        client = await factory.create(
            body.backend,
            user=user,
            model=body.model,
            model_alias=body.model_alias,
            system_prompt=body.system_prompt,
        )
        try:
            if body.session_id:
                ref = SessionRef(session_id=body.session_id, backend=Backend(body.backend))
                await client.resume_session(ref)

            msg = await client.send(body.prompt)
            _audit("agent.send", user, f"backend:{body.backend}", "execute", "success",
                   prompt_len=len(body.prompt))
            return SendResponse(text=msg.text, backend=body.backend)
        except Exception:
            _audit("agent.send", user, f"backend:{body.backend}", "execute", "error",
                   prompt_len=len(body.prompt))
            raise
        finally:
            await client.stop()

    # -- stream -----------------------------------------------------------

    @app.post("/api/v1/stream", tags=["agent"])
    async def stream(
        body: StreamRequest,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> EventSourceResponse:
        """Send a prompt and receive an SSE event stream."""

        async def _event_generator() -> AsyncGenerator[dict[str, str], None]:
            factory: ClientFactory = app.state.client_factory
            client = await factory.create(
                body.backend,
                user=user,
                model=body.model,
                model_alias=body.model_alias,
                system_prompt=body.system_prompt,
            )
            try:
                if body.session_id:
                    ref = SessionRef(session_id=body.session_id, backend=Backend(body.backend))
                    await client.resume_session(ref)

                async for chunk in client.stream(body.prompt):
                    yield {
                        "event": chunk.kind.value,
                        "data": chunk.text or chunk.tool_name or "",
                    }
            finally:
                await client.stop()

        return EventSourceResponse(_event_generator())

    # -- sessions ---------------------------------------------------------

    @app.post("/api/v1/sessions", response_model=SessionResponse, tags=["sessions"])
    async def create_session(
        body: SessionCreateRequest,
        user: AuthenticatedUser = Depends(require_role("sessions:manage")),
    ) -> SessionResponse:
        """Create a new session."""
        factory: ClientFactory = app.state.client_factory
        client = await factory.create(body.backend, user=user)
        try:
            ref = await client.create_session()
            _audit("session.create", user, f"backend:{body.backend}", "write", "success",
                   session_id=ref.session_id)
            return SessionResponse(session_id=ref.session_id, backend=ref.backend.value)
        finally:
            await client.stop()

    @app.get("/api/v1/sessions", response_model=list[SessionResponse], tags=["sessions"])
    async def list_sessions(
        user: AuthenticatedUser = Depends(require_role("sessions:manage")),
    ) -> list[SessionResponse]:
        """List available sessions across all backends."""
        results: list[SessionResponse] = []
        for backend_name in ("copilot", "claude"):
            factory: ClientFactory = app.state.client_factory
            try:
                client = await factory.create(backend_name, user=user)
                try:
                    refs = await client.list_sessions()
                    for ref in refs:
                        results.append(
                            SessionResponse(
                                session_id=ref.session_id,
                                backend=ref.backend.value,
                            )
                        )
                finally:
                    await client.stop()
            except Exception:
                logger.debug("Could not list sessions for %s", backend_name, exc_info=True)
        return results

    @app.delete("/api/v1/sessions/{session_id}", tags=["sessions"])
    async def delete_session(
        session_id: str,
        backend: str = "copilot",
        user: AuthenticatedUser = Depends(require_role("sessions:manage")),
    ) -> JSONResponse:
        """Delete a session by ID."""
        factory: ClientFactory = app.state.client_factory
        client = await factory.create(backend, user=user)
        try:
            ref = SessionRef(session_id=session_id, backend=Backend(backend))
            await client.delete_session(ref)
            _audit("session.delete", user, f"session:{session_id}", "delete", "success",
                   backend=backend)
            return JSONResponse(content={"deleted": True, "session_id": session_id})
        finally:
            await client.stop()

    # -- sync -------------------------------------------------------------

    @app.post("/api/v1/sync", response_model=SyncResponse, tags=["sync"])
    async def trigger_sync(
        body: SyncRequest,
        user: AuthenticatedUser = Depends(require_role("sync:write")),
    ) -> SyncResponse:
        """Trigger a vault sync operation.

        Runs the ``sync.py`` script as a subprocess to avoid loading
        its heavy module tree into the API server process.
        """
        cmd = [sys.executable, "sync.py", "--mode", "symlink"]
        if body.agent:
            cmd.extend(["--agent", body.agent])
        if body.repo:
            cmd.extend(["--repo", body.repo])
        if body.dry_run:
            cmd.append("--dry-run")

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            success = result.returncode == 0
            message = result.stdout.strip() if success else result.stderr.strip()
            status = "success" if success else "error"
            _audit("sync.trigger", user, "sync:vault", "execute", status,
                   agent=body.agent, repo=body.repo, dry_run=body.dry_run)
            _record_sync_metric(status)
            return SyncResponse(success=success, message=message or "sync completed")
        except subprocess.TimeoutExpired:
            _audit("sync.trigger", user, "sync:vault", "execute", "error", reason="timeout")
            _record_sync_metric("error")
            return SyncResponse(success=False, message="sync timed out after 120s")
        except Exception as exc:
            _audit("sync.trigger", user, "sync:vault", "execute", "error", reason=str(exc))
            _record_sync_metric("error")
            return SyncResponse(success=False, message=str(exc))

    # -- memory -----------------------------------------------------------

    @app.get("/api/v1/memory/{namespace}/{key}", tags=["memory"])
    async def memory_get(
        namespace: str,
        key: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Get a value from the user's memory store."""
        from sdk.memory import MemoryStore
        store = MemoryStore.for_user(user)
        value = store.get(key, namespace=namespace)
        if value is None:
            raise HTTPException(status_code=404, detail=f"Key '{namespace}:{key}' not found")
        return JSONResponse(content={"namespace": namespace, "key": key, "value": value})

    @app.post("/api/v1/memory/{namespace}/{key}", tags=["memory"])
    async def memory_set(
        namespace: str,
        key: str,
        body: dict,
        ttl: int | None = None,  # seconds
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Store a value in the user's memory store."""
        from sdk.memory import MemoryStore
        from datetime import timedelta
        store = MemoryStore.for_user(user)
        value = body.get("value")
        ttl_delta = timedelta(seconds=ttl) if ttl else None
        store.set(key, value, namespace=namespace, ttl=ttl_delta)
        _audit("memory.set", user, f"memory:{namespace}:{key}", "write", "success")
        return JSONResponse(content={"namespace": namespace, "key": key, "stored": True})

    @app.delete("/api/v1/memory/{namespace}/{key}", tags=["memory"])
    async def memory_delete(
        namespace: str,
        key: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Delete a key from the user's memory store."""
        from sdk.memory import MemoryStore
        store = MemoryStore.for_user(user)
        deleted = store.delete(key, namespace=namespace)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Key '{namespace}:{key}' not found")
        _audit("memory.delete", user, f"memory:{namespace}:{key}", "delete", "success")
        return JSONResponse(content={"namespace": namespace, "key": key, "deleted": True})

    @app.get("/api/v1/memory", tags=["memory"])
    async def memory_list(
        namespace: str | None = None,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """List all memory keys for the user."""
        from sdk.memory import MemoryStore
        store = MemoryStore.for_user(user)
        keys = store.list_keys(namespace=namespace)
        return JSONResponse(content={
            "keys": [{"namespace": k.namespace, "key": k.key} for k in keys],
            "count": len(keys),
        })

    @app.get("/api/v1/memory/search", tags=["memory"])
    async def memory_search(
        q: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Search memory keys and values."""
        from sdk.memory import MemoryStore
        store = MemoryStore.for_user(user)
        results = store.search(q)
        return JSONResponse(content={
            "results": [{"namespace": k.namespace, "key": k.key, "value": v} for k, v in results],
            "count": len(results),
        })

    @app.get("/api/v1/memory/stats", tags=["memory"])
    async def memory_stats(
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Get memory usage statistics."""
        from sdk.memory import MemoryStore
        store = MemoryStore.for_user(user)
        stats = store.get_stats()
        return JSONResponse(content=stats)

    # -- vector memory (semantic) -------------------------------------------

    @app.post("/api/v1/vector-memory/{namespace}/{key}", tags=["vector-memory"])
    async def vector_memory_set(
        namespace: str,
        key: str,
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Store text with semantic embedding for vector search."""
        from sdk.vector_memory import VectorMemoryStore
        store = VectorMemoryStore.for_user(user)
        text = body.get("text", "")
        metadata = body.get("metadata", {})
        store.set(key, text, metadata=metadata, namespace=namespace)
        _audit("vector_memory.set", user, f"vector:{namespace}:{key}", "write", "success")
        return JSONResponse(content={"namespace": namespace, "key": key, "stored": True, "type": "vector"})

    @app.get("/api/v1/vector-memory/search", tags=["vector-memory"])
    async def vector_memory_search(
        q: str,
        namespace: str | None = None,
        top_k: int = 5,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Semantic search over vector memories."""
        from sdk.vector_memory import VectorMemoryStore
        store = VectorMemoryStore.for_user(user)
        results = store.search_similar(q, namespace=namespace, top_k=top_k)
        return JSONResponse(content={
            "query": q,
            "results": [
                {
                    "namespace": r.key.namespace,
                    "key": r.key.key,
                    "text": r.text,
                    "score": r.score,
                    "metadata": r.metadata,
                }
                for r in results
            ],
            "count": len(results),
        })

    # -- agents -----------------------------------------------------------

    @app.post("/api/v1/agents", tags=["agents"])
    async def agent_spawn(
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Spawn a new agent."""
        runtime = await _get_runtime(user)

        agent = runtime.spawn(
            name=body.get("name", "unnamed"),
            model=body.get("model", "claude"),
            system_prompt=body.get("system_prompt", ""),
            memory_namespace=body.get("memory_namespace", "default"),
            max_iterations=body.get("max_iterations", 10),
        )

        await agent.start()

        _audit("agent.spawn", user, f"agent:{agent.id}", "create", "success",
               name=agent.config.name, model=agent.config.model)

        return JSONResponse(content={
            "agent_id": agent.id,
            "name": agent.config.name,
            "status": agent.status.name,
            "created_at": agent.created_at.isoformat(),
        })

    @app.get("/api/v1/agents/{agent_id}", tags=["agents"])
    async def agent_get(
        agent_id: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Get agent status and details."""
        runtime = await _get_runtime(user)
        state = runtime.get_agent_status(agent_id)
        
        if state is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        return JSONResponse(content={
            "agent_id": state.agent_id,
            "name": state.name,
            "status": state.status.name,
            "created_at": state.created_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "iteration_count": state.iteration_count,
            "error_message": state.error_message,
        })

    @app.post("/api/v1/agents/{agent_id}/run", tags=["agents"])
    async def agent_run(
        agent_id: str,
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Run a task on an existing agent."""
        runtime = await _get_runtime(user)
        agent = runtime.get_agent(agent_id)
        
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        prompt = body.get("prompt", "")
        context = body.get("context", {})
        
        try:
            result = await agent.run(prompt, **context)
            return JSONResponse(content={
                "agent_id": agent_id,
                "status": agent.status.name,
                "result": result,
            })
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/v1/agents/{agent_id}", tags=["agents"])
    async def agent_stop(
        agent_id: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Stop and cleanup an agent."""
        runtime = await _get_runtime(user)
        agent = runtime.get_agent(agent_id)
        
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        await agent.stop()
        _audit("agent.stop", user, f"agent:{agent_id}", "stop", "success")
        
        return JSONResponse(content={
            "agent_id": agent_id,
            "status": "stopped",
        })

    @app.get("/api/v1/agents", tags=["agents"])
    async def agent_list(
        status: str | None = None,
        tags: str | None = None,
        name: str | None = None,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """List all agents for the user.
        
        Query params:
            status: Filter by status (PENDING, RUNNING, WAITING, COMPLETED, FAILED, STOPPED)
            tags: Comma-separated list of tags to filter by (e.g., "production,critical")
            name: Filter by name (partial match)
        """
        from sdk.agents import AgentStatus

        runtime = await _get_runtime(user)
        
        status_filter = None
        if status:
            try:
                status_filter = AgentStatus[status.upper()]
            except KeyError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        
        agents = runtime.list_agents(status=status_filter)
        
        # Filter by tags if provided
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
            agents = [
                a for a in agents
                if any(t in getattr(a.config, "tags", []) for t in tag_list)
            ]
        
        # Filter by name if provided
        if name:
            agents = [
                a for a in agents
                if name.lower() in a.config.name.lower()
            ]
        
        return JSONResponse(content={
            "agents": [
                {
                    "agent_id": a.id,
                    "name": a.config.name,
                    "status": a.status.name,
                    "model": a.config.model,
                    "tags": getattr(a.config, "tags", []),
                    "created_at": a.created_at.isoformat(),
                }
                for a in agents
            ],
            "count": len(agents),
        })

    # -- agent bulk operations --------------------------------------------

    @app.post("/api/v1/agents/bulk", tags=["agents"])
    async def agents_bulk_spawn(
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Spawn multiple agents in one request."""
        runtime = await _get_runtime(user)
        agents_config = body.get("agents", [])
        
        if not agents_config:
            raise HTTPException(status_code=400, detail="No agents provided")
        
        if len(agents_config) > 100:
            raise HTTPException(status_code=400, detail="Cannot spawn more than 100 agents at once")
        
        created = []
        errors = []
        
        for idx, cfg in enumerate(agents_config):
            try:
                agent = runtime.spawn(
                    name=cfg.get("name", f"bulk-agent-{idx}"),
                    model=cfg.get("model", "claude"),
                    system_prompt=cfg.get("system_prompt", ""),
                    memory_namespace=cfg.get("memory_namespace", "default"),
                    max_iterations=cfg.get("max_iterations", 10),
                    tags=cfg.get("tags", []),
                )
                await agent.start()
                
                created.append({
                    "agent_id": agent.id,
                    "name": agent.config.name,
                    "status": agent.status.name,
                })
                
                _audit("agent.spawn", user, f"agent:{agent.id}", "create", "success",
                       name=agent.config.name, model=agent.config.model, bulk=True)
                       
            except Exception as e:
                errors.append({"index": idx, "name": cfg.get("name"), "error": str(e)})
        
        return JSONResponse(content={
            "created": created,
            "errors": errors,
            "total_requested": len(agents_config),
            "total_created": len(created),
        })

    @app.post("/api/v1/agents/bulk/stop", tags=["agents"])
    async def agents_bulk_stop(
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Stop multiple agents in one request."""
        runtime = await _get_runtime(user)
        agent_ids = body.get("agent_ids", [])
        
        if not agent_ids:
            raise HTTPException(status_code=400, detail="No agent_ids provided")
        
        stopped = []
        errors = []
        
        for agent_id in agent_ids:
            try:
                agent = runtime.get_agent(agent_id)
                if agent is None:
                    errors.append({"agent_id": agent_id, "error": "Agent not found"})
                    continue
                    
                await agent.stop()
                stopped.append(agent_id)
                _audit("agent.stop", user, f"agent:{agent_id}", "stop", "success", bulk=True)
                
            except Exception as e:
                errors.append({"agent_id": agent_id, "error": str(e)})
        
        return JSONResponse(content={
            "stopped": stopped,
            "errors": errors,
            "total_requested": len(agent_ids),
            "total_stopped": len(stopped),
        })

    @app.post("/api/v1/agents/bulk/tag", tags=["agents"])
    async def agents_bulk_tag(
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Add tags to multiple agents."""
        runtime = await _get_runtime(user)
        agent_ids = body.get("agent_ids", [])
        tags = body.get("tags", [])
        
        if not agent_ids:
            raise HTTPException(status_code=400, detail="No agent_ids provided")
        
        if not tags:
            raise HTTPException(status_code=400, detail="No tags provided")
        
        tagged = []
        errors = []
        
        for agent_id in agent_ids:
            try:
                agent = runtime.get_agent(agent_id)
                if agent is None:
                    errors.append({"agent_id": agent_id, "error": "Agent not found"})
                    continue
                
                # Add tags to agent config if supported
                current_tags = getattr(agent.config, "tags", [])
                new_tags = list(set(current_tags + tags))
                agent.config.tags = new_tags
                
                tagged.append(agent_id)
                
            except Exception as e:
                errors.append({"agent_id": agent_id, "error": str(e)})
        
        return JSONResponse(content={
            "tagged": tagged,
            "errors": errors,
            "tags": tags,
        })

    # -- agent templates --------------------------------------------------

    _agent_templates: dict[str, dict] = {}

    @app.post("/api/v1/agent-templates", tags=["agents"])
    async def template_create(
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Create an agent template."""
        import uuid
        
        template_id = str(uuid.uuid4())
        template = {
            "template_id": template_id,
            "name": body.get("name", "unnamed-template"),
            "model": body.get("model", "claude"),
            "system_prompt": body.get("system_prompt", ""),
            "timeout_seconds": body.get("timeout_seconds", 300),
            "max_iterations": body.get("max_iterations", 10),
            "memory_namespace": body.get("memory_namespace", "default"),
            "tags": body.get("tags", []),
            "created_by": user.user_id,
            "created_at": datetime.utcnow().isoformat(),
        }
        
        _agent_templates[template_id] = template
        
        _audit("template.create", user, f"template:{template_id}", "create", "success",
               name=template["name"])
        
        return JSONResponse(content=template)

    @app.get("/api/v1/agent-templates", tags=["agents"])
    async def template_list(
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """List all agent templates."""
        templates = list(_agent_templates.values())
        return JSONResponse(content={
            "templates": templates,
            "count": len(templates),
        })

    @app.get("/api/v1/agent-templates/{template_id}", tags=["agents"])
    async def template_get(
        template_id: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Get a specific agent template."""
        template = _agent_templates.get(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
        return JSONResponse(content=template)

    @app.delete("/api/v1/agent-templates/{template_id}", tags=["agents"])
    async def template_delete(
        template_id: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Delete an agent template."""
        if template_id not in _agent_templates:
            raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
        
        del _agent_templates[template_id]
        
        _audit("template.delete", user, f"template:{template_id}", "delete", "success")
        
        return JSONResponse(content={"template_id": template_id, "deleted": True})

    @app.post("/api/v1/agents/from-template", tags=["agents"])
    async def agent_spawn_from_template(
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Spawn an agent from a template."""
        runtime = await _get_runtime(user)
        template_id = body.get("template_id")
        
        if not template_id:
            raise HTTPException(status_code=400, detail="template_id is required")
        
        template = _agent_templates.get(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
        
        # Override template values with request values
        agent = runtime.spawn(
            name=body.get("name", f"{template['name']}-instance"),
            model=template.get("model", "claude"),
            system_prompt=template.get("system_prompt", ""),
            memory_namespace=template.get("memory_namespace", "default"),
            max_iterations=template.get("max_iterations", 10),
            tags=template.get("tags", []),
        )
        
        await agent.start()
        
        _audit("agent.spawn", user, f"agent:{agent.id}", "create", "success",
               name=agent.config.name, template_id=template_id)
        
        return JSONResponse(content={
            "agent_id": agent.id,
            "name": agent.config.name,
            "status": agent.status.name,
            "template_id": template_id,
            "created_at": agent.created_at.isoformat(),
        })

    # -- agent tags -------------------------------------------------------

    @app.post("/api/v1/agents/{agent_id}/tags", tags=["agents"])
    async def agent_add_tags(
        agent_id: str,
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Add tags to an agent."""
        runtime = await _get_runtime(user)
        agent = runtime.get_agent(agent_id)
        
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        tags = body.get("tags", [])
        if not tags:
            raise HTTPException(status_code=400, detail="No tags provided")
        
        # Initialize tags if not present
        if not hasattr(agent.config, "tags"):
            agent.config.tags = []
        
        # Add new tags
        current_tags = set(agent.config.tags)
        new_tags = set(tags)
        agent.config.tags = list(current_tags | new_tags)
        
        return JSONResponse(content={
            "agent_id": agent_id,
            "tags": agent.config.tags,
            "added": list(new_tags - current_tags),
        })

    @app.post("/api/v1/agents/{agent_id}/tags/remove", tags=["agents"])
    async def agent_remove_tags(
        agent_id: str,
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> JSONResponse:
        """Remove tags from an agent."""
        runtime = await _get_runtime(user)
        agent = runtime.get_agent(agent_id)
        
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        tags = body.get("tags", [])
        if not tags:
            raise HTTPException(status_code=400, detail="No tags provided")
        
        if not hasattr(agent.config, "tags"):
            return JSONResponse(content={"agent_id": agent_id, "tags": [], "removed": []})
        
        current_tags = set(agent.config.tags)
        remove_tags = set(tags)
        agent.config.tags = list(current_tags - remove_tags)
        
        return JSONResponse(content={
            "agent_id": agent_id,
            "tags": agent.config.tags,
            "removed": list(remove_tags & current_tags),
        })

    @app.get("/api/v1/agents/{agent_id}/tags", tags=["agents"])
    async def agent_get_tags(
        agent_id: str,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude", "agent:read")),
    ) -> JSONResponse:
        """Get tags for an agent."""
        runtime = await _get_runtime(user)
        agent = runtime.get_agent(agent_id)
        
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        tags = getattr(agent.config, "tags", [])
        
        return JSONResponse(content={
            "agent_id": agent_id,
            "tags": tags,
        })

    # -- agent stream (SSE) -----------------------------------------------

    @app.post("/api/v1/agents/{agent_id}/stream", tags=["agents"])
    async def agent_stream(
        agent_id: str,
        body: dict,
        user: AuthenticatedUser = Depends(require_any_role("agent:copilot", "agent:claude")),
    ) -> EventSourceResponse:
        """Stream an agent's response as SSE events."""
        runtime = await _get_runtime(user)
        agent = runtime.get_agent(agent_id)

        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

        prompt = body.get("prompt", "")
        context = body.get("context", {})

        async def _event_generator() -> AsyncGenerator[dict[str, str], None]:
            try:
                async for chunk in agent.stream(prompt, **context):
                    yield {"event": "chunk", "data": chunk}
                yield {"event": "done", "data": ""}
            except Exception as e:
                yield {"event": "error", "data": str(e)}

        return EventSourceResponse(_event_generator())

    # -- websockets -------------------------------------------------------

    from fastapi import WebSocket, WebSocketDisconnect

    async def _authenticate_websocket(websocket: WebSocket) -> AuthenticatedUser | None:
        """Validate JWT token from WebSocket query params."""
        token = websocket.query_params.get("token", "")
        if not token:
            return None

        if not config.auth_enabled:
            # Auth disabled — use a default local dev user
            return AuthenticatedUser(
                user_id="local-dev",
                email="dev@obscura.dev",
                roles=("agent:copilot", "agent:claude", "agent:read"),
                org_id="local",
                token_type="user",
                raw_token=token,
            )

        try:
            jwks: JWKSCache = app.state.jwks_cache
            import jwt as pyjwt
            payload = pyjwt.decode(
                token,
                jwks.keys,
                algorithms=["RS256"],
                audience=config.auth_audience,
                issuer=config.auth_issuer,
            )
            return AuthenticatedUser(
                user_id=payload.get("sub", "unknown"),
                email=payload.get("email", ""),
                roles=tuple(payload.get("urn:zitadel:iam:org:project:roles", {}).keys()) or ("agent:read",),
                org_id=payload.get("org_id", ""),
                token_type="user",
                raw_token=token,
            )
        except Exception:
            logger.warning("WebSocket auth failed for token")
            return None

    @app.websocket("/ws/agents/{agent_id}")
    async def agent_websocket(
        websocket: WebSocket,
        agent_id: str,
    ):
        """
        WebSocket endpoint for real-time agent communication.

        Protocol:
        - Client connects with auth token in query param: ?token=xxx
        - Server accepts connection
        - Client sends: {"type": "run", "prompt": "...", "context": {}}
        - Server streams: {"type": "chunk", "text": "..."}
        - Server sends: {"type": "done"} or {"type": "error", "message": "..."}
        """
        user = await _authenticate_websocket(websocket)
        if user is None:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        await websocket.accept()

        try:
            runtime = await _get_runtime(user)
            agent = runtime.get_agent(agent_id)
            
            if agent is None:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Agent {agent_id} not found"
                })
                await websocket.close()
                return
            
            while True:
                # Wait for message from client
                message = await websocket.receive_json()
                
                if message.get("type") == "run":
                    prompt = message.get("prompt", "")
                    context = message.get("context", {})
                    
                    # Stream the response
                    try:
                        async for chunk in agent.stream(prompt, **context):
                            await websocket.send_json({
                                "type": "chunk",
                                "text": chunk,
                            })
                        
                        await websocket.send_json({"type": "done"})
                        
                    except Exception as e:
                        await websocket.send_json({
                            "type": "error",
                            "message": str(e),
                        })
                
                elif message.get("type") == "status":
                    state = agent.get_state()
                    await websocket.send_json({
                        "type": "status",
                        "status": state.status.name,
                        "iteration_count": state.iteration_count,
                    })
                
                elif message.get("type") == "stop":
                    await agent.stop()
                    await websocket.send_json({
                        "type": "status",
                        "status": "STOPPED",
                    })
                    break
                    
        except WebSocketDisconnect:
            pass
        except Exception as e:
            try:
                await websocket.send_json({
                    "type": "error",
                    "message": str(e),
                })
            except:
                pass

    @app.websocket("/ws/monitor")
    async def monitor_websocket(websocket: WebSocket):
        """
        WebSocket endpoint for monitoring all agents.

        Streams real-time updates about agent status changes.
        """
        user = await _authenticate_websocket(websocket)
        if user is None:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        await websocket.accept()

        try:
            runtime = await _get_runtime(user)
            
            # Send initial state
            agents = runtime.list_agents()
            await websocket.send_json({
                "type": "init",
                "agents": [
                    {
                        "agent_id": a.id,
                        "name": a.config.name,
                        "status": a.status.name,
                        "model": a.config.model,
                    }
                    for a in agents
                ],
            })
            
            # Keep connection alive and send periodic updates
            while True:
                await asyncio.sleep(5)  # Update every 5 seconds
                
                agents = runtime.list_agents()
                await websocket.send_json({
                    "type": "update",
                    "agents": [
                        {
                            "agent_id": a.id,
                            "name": a.config.name,
                            "status": a.status.name,
                            "model": a.config.model,
                        }
                        for a in agents
                    ],
                })
                
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    return app

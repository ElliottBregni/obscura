"""obscura.a2a.standalone — Standalone A2A server configuration and factory.

Two APIs are provided:

1. **Static-card utilities** (original API, preserved for back-compat):
   :func:`load_static_card`, :func:`apply_server_url`, :func:`apply_peers`,
   :func:`build_runtime_card`, :func:`load_peers_from_file`, and the thin
   :class:`PeerAgent` / :class:`StandaloneA2AConfig` (file-path-based) dataclasses.

2. **Programmatic server factory** (new API):
   :class:`ServerConfig` + :func:`build_standalone_server` — build a fully
   wired :class:`~obscura.integrations.a2a.server.ObscuraA2AServer` from a
   plain dataclass, no static JSON files required.

OpenClaw compatibility
----------------------
When ``ServerConfig.openclaw_compat=True`` the factory:

* Adds a ``"bearer"`` HTTP security scheme named ``"openclaw"`` to the card.
* Injects the ``"openclaw"`` tag into every advertised skill.
* Sets ``protocolVersion`` to ``"0.3"`` (OpenClaw's expected version).
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.types import AgentSkill, AuthScheme
from obscura.integrations.a2a.well_known import WellKnownAgentRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from obscura.integrations.a2a.definition import AgentDefinition
    from obscura.integrations.a2a.server import ObscuraA2AServer
    from obscura.integrations.a2a.service import A2AService
    from obscura.integrations.a2a.store import TaskStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Original static-card dataclasses (preserved for back-compat)
# ---------------------------------------------------------------------------


@dataclass
class PeerAgent:
    """A remote A2A peer agent."""

    name: str
    url: str
    description: str = ""
    role: str = "general"
    card_url: str = ""

    def __post_init__(self) -> None:
        if not self.card_url:
            self.card_url = f"{self.url}/.well-known/agent.json"


@dataclass
class StandaloneA2AConfig:
    """Configuration for standalone A2A card serving (file-path-based).

    .. deprecated::
        Prefer :class:`ServerConfig` + :func:`build_standalone_server` for
        programmatic server setup.
    """

    agent_card_path: Path
    peers: list[PeerAgent] = field(default_factory=list)
    server_url: str = "http://localhost:8080"
    openclaw_team: str = "default"
    openclaw_role: str = "general"


def load_static_card(path: Path) -> dict[str, Any]:
    """Read and parse the static agent card JSON from *path*.

    Raises:
        FileNotFoundError: if *path* does not exist, with a helpful message.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Agent card not found at {path}. "
            "Create it or copy .well-known/agent.json from the repo root."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def apply_server_url(card: dict[str, Any], url: str) -> dict[str, Any]:
    """Return a copy of *card* with the top-level ``url`` field set to *url*."""
    updated = copy.deepcopy(card)
    updated["url"] = url
    return updated


def apply_peers(card: dict[str, Any], peers: list[PeerAgent]) -> dict[str, Any]:
    """Return a copy of *card* with the openclaw-discovery extension peers populated.

    Finds the extension whose ``id`` is ``"openclaw-discovery"`` and sets its
    ``params.peers`` to the serialised form of *peers*.  If no such extension
    exists the card is returned unchanged.
    """
    updated = copy.deepcopy(card)
    extensions: list[dict[str, Any]] = updated.get("extensions") or []
    for ext in extensions:
        if ext.get("id") == "openclaw-discovery":
            params: dict[str, Any] = ext.setdefault("params", {})
            params["peers"] = [
                {
                    "name": p.name,
                    "url": p.url,
                    "role": p.role,
                    "cardUrl": p.card_url or f"{p.url}/.well-known/agent.json",
                }
                for p in peers
            ]
            break
    return updated


def build_runtime_card(config: StandaloneA2AConfig) -> dict[str, Any]:
    """Build the runtime agent card from *config*.

    Loads the static card, patches the server URL, and injects peer agents into
    the OpenClaw discovery extension.
    """
    card = load_static_card(config.agent_card_path)
    card = apply_server_url(card, config.server_url)
    card = apply_peers(card, config.peers)
    return card


def load_peers_from_file(path: Path) -> list[PeerAgent]:
    """Load peer agents from a JSON file.

    Expected format::

        {
          "peers": [
            {"name": "...", "url": "...", "description": "...", "role": "..."}
          ]
        }

    Returns an empty list if *path* does not exist.
    """
    if not path.exists():
        return []
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return [
        PeerAgent(
            name=entry["name"],
            url=entry["url"],
            description=entry.get("description", ""),
            role=entry.get("role", "general"),
            card_url=entry.get("card_url", ""),
        )
        for entry in data.get("peers", [])
    ]


# ---------------------------------------------------------------------------
# New programmatic server-config API
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    """Full programmatic configuration for a standalone Obscura A2A server.

    Parameters
    ----------
    name:
        Agent display name (appears in ``/.well-known/agent.json``).
    description:
        Human-readable description of the agent.
    host:
        Interface to bind (``"0.0.0.0"`` for all interfaces).
    port:
        TCP port to listen on. ``0`` lets the OS pick a free port (useful for tests).
    version:
        Agent version string.
    protocol_version:
        A2A protocol version to advertise.
    bearer_tokens:
        List of accepted bearer tokens.  Requests without a matching token
        receive a ``401`` response.  An empty list disables token validation.
    skills:
        Skills to advertise in the agent card.  Each entry is a dict with
        keys: ``id``, ``name``, ``description``, ``tags`` (list),
        ``examples`` (list).
    well_known_agents:
        Peers to pre-register in the
        :class:`~obscura.integrations.a2a.well_known.WellKnownAgentRegistry`.
        Each entry is a dict with keys: ``name``, ``url``, ``description``,
        ``auth_token`` (optional).
    streaming:
        Whether the agent supports streaming (SSE).
    push_notifications:
        Whether the agent supports push notifications.
    openclaw_compat:
        When ``True``:

        * Adds a ``"bearer"`` HTTP security scheme named ``"openclaw"``
          to the agent card.
        * Injects the ``"openclaw"`` tag into every skill.
        * Sets ``protocolVersion`` to ``"0.3"`` (OpenClaw's expected version).

    """

    name: str = "Obscura"
    description: str = "Obscura AI agent runtime"
    host: str = "0.0.0.0"
    port: int = 8080
    version: str = "1.0"
    protocol_version: str = "0.3"
    # Auth
    bearer_tokens: list[str] = field(default_factory=list)
    # Skills exposed
    skills: list[dict[str, Any]] = field(default_factory=list)
    # Well-known peers to register
    well_known_agents: list[dict[str, Any]] = field(default_factory=list)
    # Capabilities
    streaming: bool = True
    push_notifications: bool = False
    # OpenClaw compatibility
    openclaw_compat: bool = True


def _make_token_validator(tokens: list[str]) -> Callable[[str], bool] | None:
    """Return a validator callable, or ``None`` if *tokens* is empty."""
    if not tokens:
        return None
    token_set: frozenset[str] = frozenset(tokens)

    def _validate(token: str) -> bool:
        return token in token_set

    return _validate


def _build_skills(
    raw: list[dict[str, Any]],
    *,
    inject_openclaw_tag: bool,
) -> list[AgentSkill]:
    """Convert raw skill dicts to :class:`AgentSkill` objects."""
    skills: list[AgentSkill] = []
    for entry in raw:
        tags: list[str] = list(entry.get("tags") or [])
        if inject_openclaw_tag and "openclaw" not in tags:
            tags.append("openclaw")
        skills.append(
            AgentSkill(
                id=str(entry.get("id", entry.get("name", ""))),
                name=str(entry.get("name", "")),
                description=str(entry.get("description", "")),
                tags=tags,
                examples=list(entry.get("examples") or []),
            )
        )
    return skills


async def build_standalone_server(
    config: ServerConfig,
    service: A2AService,
) -> ObscuraA2AServer:
    """Build and return a configured :class:`~obscura.integrations.a2a.server.ObscuraA2AServer`.

    Steps
    -----
    1. Build an :class:`~obscura.integrations.a2a.agent_card.AgentCardGenerator`
       from *config*.
    2. If ``openclaw_compat=True``, add the ``"openclaw"`` bearer auth scheme
       and inject ``"openclaw"`` into all skill tags.
    3. Construct a token-validator callable from ``config.bearer_tokens``.
    4. Build a :class:`~obscura.integrations.a2a.well_known.WellKnownAgentRegistry`
       from ``config.well_known_agents`` and attach it as
       ``server.peer_registry``.
    5. Return the configured server.

    Parameters
    ----------
    config:
        Standalone server configuration.
    service:
        Pre-constructed :class:`~obscura.integrations.a2a.service.A2AService`
        instance that handles task execution.

    """
    from obscura.integrations.a2a.server import ObscuraA2AServer

    base_url = f"http://{config.host}:{config.port}"

    # ----- Agent card -----
    gen = AgentCardGenerator(
        name=config.name,
        url=base_url,
        description=config.description,
        version=config.version,
    )

    skills = _build_skills(config.skills, inject_openclaw_tag=config.openclaw_compat)
    if skills:
        gen.with_skills(skills)

    gen.with_capabilities(
        streaming=config.streaming,
        push_notifications=config.push_notifications,
    )

    if config.openclaw_compat:
        gen.with_auth_scheme("openclaw", AuthScheme(type="http", scheme="bearer"))
        logger.debug("OpenClaw compat: added 'openclaw' bearer auth scheme")
    elif config.bearer_tokens:
        gen.with_bearer_auth()

    gen.with_provider("Obscura", "https://obscura.dev")
    agent_card = gen.build()

    # ----- Token validator -----
    token_validator = _make_token_validator(config.bearer_tokens)

    # ----- Peer registry -----
    peer_registry = WellKnownAgentRegistry.from_config(
        {"well_known_agents": config.well_known_agents}
    )
    logger.debug(
        "Well-known peer registry: %d agents loaded",
        len(peer_registry.list()),
    )

    # ----- Build server -----
    server = ObscuraA2AServer(
        store=service.store,
        agent_card=agent_card,
    )

    # Attach token validator for use by auth middleware if needed.
    # ObscuraA2AServer does not wire auth internally; callers should install
    # APIKeyAuthMiddleware or a custom middleware that calls this validator.
    server._token_validator = token_validator  # type: ignore[attr-defined]

    # Attach peer registry as a convenience attribute
    server.peer_registry = peer_registry  # type: ignore[attr-defined]

    logger.info(
        "Standalone A2A server configured: name=%r port=%d openclaw_compat=%s tokens=%d",
        config.name,
        config.port,
        config.openclaw_compat,
        len(config.bearer_tokens),
    )
    return server


# ---------------------------------------------------------------------------
# FastAPI app factory  (uvicorn-compatible entry point)
# ---------------------------------------------------------------------------


def _make_lifespan(a2a_server: ObscuraA2AServer) -> Callable:  # type: ignore[type-arg]
    """Return an asynccontextmanager lifespan that manages *a2a_server*."""
    from contextlib import asynccontextmanager

    @asynccontextmanager  # type: ignore[arg-type]
    async def lifespan(app: Any) -> Any:  # noqa: ARG001
        await a2a_server.startup()
        try:
            yield
        finally:
            await a2a_server.shutdown()

    return lifespan


def create_standalone_app(
    definition: "AgentDefinition | None" = None,
    *,
    base_url: str = "http://localhost:8080",
    store: "TaskStore | None" = None,
    agent_backend: str = "copilot",
    agent_model: str = "",
    agent_system_prompt: str = "",
    unix_socket_path: str | None = None,
    enable_auth: bool = True,
) -> "FastAPI":
    """Create a standalone FastAPI application exposing the A2A protocol.

    All four A2A transport routers (JSON-RPC, REST, SSE, well-known) are
    mounted on the returned application. The app's ``state.a2a_server``
    attribute holds the underlying :class:`ObscuraA2AServer` instance so
    callers can access it after startup.

    Parameters
    ----------
    definition:
        Agent definition that controls the well-known card. Defaults to
        :data:`~obscura.integrations.a2a.definition.DEFAULT_AGENT_DEFINITION`
        when not provided.
    base_url:
        Public base URL for the agent (embedded in the agent card ``url``
        field and returned to callers).
    store:
        Task persistence backend. Defaults to :class:`InMemoryTaskStore`.
    agent_backend:
        LLM provider identifier (e.g. ``"copilot"``, ``"claude"``).
    agent_model:
        Default model override for spawned agent sessions.
    agent_system_prompt:
        Default system prompt for spawned agent sessions.
    unix_socket_path:
        Optional Unix domain socket path. When set, a socket transport is
        started alongside the HTTP server during lifespan startup.
    enable_auth:
        If ``True`` (the default), installs
        :class:`~obscura.integrations.a2a.auth.A2ABearerAuthMiddleware` to
        require a valid ``Authorization: Bearer`` token on all ``/a2a/*``
        paths.  ``/.well-known/agent.json`` remains publicly accessible.
        Tokens are read from ``OBSCURA_A2A_TOKEN`` env var or
        ``~/.obscura/a2a-gateway.token``.  Pass ``False`` only in trusted
        local / test environments.

    Returns
    -------
    FastAPI
        Fully configured FastAPI application.

    Security middleware stack (outermost → innermost):
        1. :class:`~obscura.auth.security_headers.SecurityHeadersMiddleware`
        2. :class:`~obscura.integrations.a2a.auth.A2ARateLimitMiddleware`
           (60 req/min per IP)
        3. :class:`~obscura.integrations.a2a.auth.A2ABearerAuthMiddleware`
           (when ``enable_auth=True``)
        4. CORS
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from obscura.auth.security_headers import SecurityHeadersMiddleware
    from obscura.integrations.a2a.definition import (
        DEFAULT_AGENT_DEFINITION,
    )
    from obscura.integrations.a2a.server import ObscuraA2AServer as _ObscuraA2AServer
    from obscura.integrations.a2a.transports import (
        create_jsonrpc_router,
        create_rest_router,
        create_sse_router,
        create_wellknown_router,
    )

    effective_definition: AgentDefinition = (
        DEFAULT_AGENT_DEFINITION if definition is None else definition
    )

    card = effective_definition.to_agent_card(base_url)

    a2a_server = _ObscuraA2AServer(
        store=store,
        agent_card=card,
        agent_backend=agent_backend,
        agent_model=agent_model,
        agent_system_prompt=agent_system_prompt,
        unix_socket_path=unix_socket_path,
    )

    fastapi_app = FastAPI(
        title=effective_definition.name,
        description=effective_definition.description,
        version=effective_definition.version,
        lifespan=_make_lifespan(a2a_server),
    )

    # ------------------------------------------------------------------ #
    # Middleware stack — add_middleware() wraps in LIFO order, so the last
    # .add_middleware() call becomes the outermost layer.
    # Desired request flow:
    #   SecurityHeaders → RateLimit → BearerAuth → CORS → routes
    # Therefore registration order is CORS first, then each layer in reverse.
    # ------------------------------------------------------------------ #

    # CORS — open by default for A2A interop; restrict in production via a
    # reverse-proxy or by passing a tighter allow_origins list.
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Bearer auth — guards /a2a/* (/.well-known/ is always public).
    if enable_auth:
        from obscura.integrations.a2a.auth import A2ABearerAuthMiddleware

        fastapi_app.add_middleware(A2ABearerAuthMiddleware)

    # Per-IP rate limit — 60 requests/minute on /a2a/* paths.
    from obscura.integrations.a2a.auth import A2ARateLimitMiddleware

    fastapi_app.add_middleware(A2ARateLimitMiddleware)

    # Security headers — applied to every response (outermost layer).
    fastapi_app.add_middleware(SecurityHeadersMiddleware)

    service = a2a_server.service

    fastapi_app.include_router(create_jsonrpc_router(service))
    fastapi_app.include_router(create_rest_router(service))
    fastapi_app.include_router(create_sse_router(service))
    fastapi_app.include_router(create_wellknown_router(service))

    # ------------------------------------------------------------------ #
    # Webhook receiver — peer agents (e.g. OpenClaw) POST async results here
    # ------------------------------------------------------------------ #
    _register_webhook_endpoints(fastapi_app)

    fastapi_app.state.a2a_server = a2a_server

    return fastapi_app


def _register_webhook_endpoints(app: "FastAPI") -> None:
    """Register the ``POST /webhook/a2a`` endpoint on *app*.

    This endpoint receives async A2A task results pushed by peer agents
    (e.g. OpenClaw).  It is public — peer agents on loopback have no
    Obscura bearer token in the webhook callback context.
    """
    import logging as _logging

    from fastapi import Request

    _wh_logger = _logging.getLogger(__name__ + ".webhook")

    @app.post("/webhook/a2a", tags=["webhooks"], include_in_schema=False)
    async def receive_a2a_webhook(request: Request) -> dict[str, Any]:
        """Receive async A2A task results pushed from peer agents (e.g. OpenClaw)."""
        try:
            body = await request.json()
            task_id: str = body.get("id", "unknown")
            state: str = body.get("status", {}).get("state", "unknown")
            _wh_logger.info(
                "A2A webhook received: task_id=%s state=%s", task_id, state
            )

            # Extract text from artifacts and push into channel inject queue
            artifacts: list[Any] = body.get("artifacts", [])
            text_parts: list[str] = []
            for art in artifacts:
                for part in art.get("parts", []):
                    if part.get("type") == "text" and part.get("text"):
                        text_parts.append(part["text"])
            text = "\n".join(text_parts)

            if text:
                try:
                    from obscura.integrations.messaging.channel_inject import (
                        ChannelMessage,
                        push_channel_message,
                    )

                    async def _noop(t: str) -> bool:  # noqa: ARG001
                        return True

                    push_channel_message(
                        ChannelMessage(
                            platform="a2a-webhook",
                            sender_id=body.get("metadata", {}).get(
                                "from", "peer-agent"
                            ),
                            text=(
                                f"[A2A result task={task_id[:8]} state={state}]: {text}"
                            ),
                            reply_fn=_noop,
                        )
                    )
                except Exception:
                    _wh_logger.debug(
                        "webhook: channel inject unavailable", exc_info=True
                    )

            return {"ok": True, "task_id": task_id}
        except Exception:
            _wh_logger.exception("A2A webhook parse error")
            return {"ok": False}


# Module-level app — enables ``uvicorn obscura.integrations.a2a.standalone:app``
app: "FastAPI" = create_standalone_app()

__all__ = [
    # Original API
    "PeerAgent",
    "StandaloneA2AConfig",
    "apply_peers",
    "apply_server_url",
    "build_runtime_card",
    "load_peers_from_file",
    "load_static_card",
    # New programmatic server config API
    "ServerConfig",
    "build_standalone_server",
    # FastAPI app factory
    "app",
    "create_standalone_app",
]

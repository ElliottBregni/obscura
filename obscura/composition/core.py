"""obscura.composition.core — surface-agnostic core composition.

`build_core_session(config, *, surface, ...)` constructs the agent's
backend, tool registry, and capability state DIRECTLY — without going
through ObscuraClient. This is Stage 4b of the ObscuraClient
absorption: composition path no longer instantiates ObscuraClient.

The returned ``AgentSession`` carries the backend on
``_owned_backend`` and the tool registry on ``_owned_tool_registry``;
its ``backend`` / ``registry`` properties prefer those over the legacy
``client._backend`` / ``client._tool_registry`` paths. ``client``
remains as an Optional field used only by the Agent.start legacy
construction path.

What core does:
- env bootstrap (idempotent — bootstrap_env())
- Resolve backend type, model, auth
- Build ToolRegistry, register pre-supplied tools
- Generate identity (capability) token (composition.tokens)
- Construct backend via composition.backend_factory.create_backend
- Run install_mcp_servers BEFORE backend.start (so Claude SDK sees them)
- Run install_plugin_tools + install_system_tools BEFORE backend.start
  (Copilot and Claude SDKs commit the tool list at session creation)
- await backend.start()
- Return AgentSession with _owned_* state populated

What core does NOT do (extras blocks add):
- vector memory (install_vector_memory)
- hook loading (install_project_hooks)
- skill context (install_skill_context)
- supervisor / KAIROS / browser bridge / iMessage daemon (REPL extras)

MCP server connection is performed by the canonical
``install_mcp_servers`` block (composition/blocks/mcp_servers.py),
called once from here. Surface modules (REPL/API/A2A) MUST NOT call
the block again — core has already done it. The block is idempotent
(``session.add_tool`` de-dupes by name) so a stray repeat call is
harmless, but creates a redundant MCPBackend connection that wastes
sockets.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    Surface,
    new_session_id,
)

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)


def _resolve_model(
    backend: Any,
    model: str | None,
    model_alias: str | None = None,
) -> str | None:
    """Resolve a model name from alias or pass-through. Mirrors
    ``ObscuraClient._resolve_model``."""
    from obscura.core.enums.agent import Backend

    if model_alias is not None and backend == Backend.COPILOT:
        return model_alias
    if model_alias is not None and model is None:
        return model_alias
    return model


async def build_core_session(
    config: SessionConfig,
    *,
    surface: Surface,
    user: AuthenticatedUser | None = None,
    host_callbacks: dict[str, Any] | None = None,
    auth: Any = None,
    session_id: str | None = None,
    preregistered_tools: list[Any] | None = None,
    hooks: Any = None,
) -> AgentSession:
    """Construct the surface-agnostic core of an `AgentSession`.

    Composition path: builds backend directly via ``create_backend``
    (no ObscuraClient instantiation). Per-surface boot modules call
    this, then run their extras pipelines.
    """
    # 1. .env load — idempotent
    from obscura.cli._env_loader import bootstrap_env

    bootstrap_env()

    # 2. Resolve backend type + model + auth
    from obscura.composition.backend_factory import create_backend
    from obscura.composition.tokens import (
        generate_identity_token,
        maybe_inject_tier_prompt,
    )
    from obscura.core.auth import resolve_auth
    from obscura.core.circuit_breaker import CircuitBreakerRegistry
    from obscura.core.enums.agent import Backend
    from obscura.core.tool_policy import ToolPolicy
    from obscura.core.tools import ToolRegistry

    sid = session_id or new_session_id()
    callbacks = host_callbacks or {}

    backend_kind = Backend(config.backend) if config.backend else Backend.COPILOT
    resolved_model = _resolve_model(backend_kind, config.model)
    resolved_auth = resolve_auth(backend_kind, auth, user=user)

    # 3. Identity / capability token (drives capability gate + tier prompt)
    capability_token = generate_identity_token(user, sid)
    effective_prompt = maybe_inject_tier_prompt(
        capability_token,
        config.system_prompt,
    )

    # 4. Build tool registry, pre-register caller-supplied tools
    tool_registry = ToolRegistry()
    for spec in preregistered_tools or []:
        tool_registry.register(spec)

    # 5. MCP server routing — Codex gets configs forwarded to its SDK,
    # everyone else gets MCP via MCPBackend (connected below).
    from obscura.core.backend_features import backend_routes_mcp_natively

    native_mcp = backend_routes_mcp_natively(backend_kind)
    backend_mcp_servers = config.mcp_servers if native_mcp else None

    # 6. Construct backend (composition.backend_factory.create_backend
    # extracted from ObscuraClient._create_backend earlier)
    tool_policy = ToolPolicy.custom_only()
    backend = create_backend(
        backend=backend_kind,
        auth=resolved_auth,
        model=resolved_model,
        system_prompt=effective_prompt,
        mcp_servers=backend_mcp_servers,
        permission_mode="default",
        cwd=None,
        streaming=True,
        tool_policy=tool_policy,
    )

    # Register pre-supplied tools with backend so it sees them at start
    for spec in tool_registry.all():
        backend.register_tool(spec)

    # 7. Build session with owned state populated. Construct BEFORE
    # backend.start() so install_mcp_servers can register MCP tools on
    # the backend in time for Claude SDK initialisation (Claude reads
    # self._tools when building SDK options).
    session = AgentSession(
        session_id=sid,
        surface=surface,
        config=config,
        client=None,  # composition path — no ObscuraClient
        host_callbacks=dict(callbacks),
        system_prompt=effective_prompt,
        _owned_backend=backend,
        _owned_tool_registry=tool_registry,
        _owned_hooks=hooks,
        _owned_user=user,
        _owned_system_prompt=effective_prompt,
        _capability_token=capability_token,
        _circuit_registry=CircuitBreakerRegistry(),
    )

    # 8. Connect MCP servers via the canonical block. Codex skips this
    # block (it owns its own MCP routing via the SDK config). Surface
    # modules MUST NOT call this block again — core has done it.
    from obscura.composition.blocks.mcp_servers import install_mcp_servers

    await install_mcp_servers(session, config)

    # 9. Register plugin + system tools BEFORE backend.start so the
    # Copilot and Claude SDKs (which commit the tool list at session
    # creation) see them. Surfaces MUST NOT call these blocks again —
    # core has done it. Both blocks are idempotent (session.add_tool
    # dedupes by name) but a stray repeat call wastes work.
    from obscura.composition.blocks.plugins import install_plugin_tools
    from obscura.composition.blocks.system_tools import install_system_tools

    await install_plugin_tools(session, config)
    await install_system_tools(session, config)

    # 10. Start backend
    try:
        await backend.start()
    except Exception:
        logger.exception("build_core_session: backend.start failed")
        raise

    return session

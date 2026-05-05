"""obscura.composition.core — surface-agnostic core composition.

`build_core_session(config, *, surface, ...)` constructs the shared
`ObscuraClient` and wraps it in an `AgentSession`. Each per-surface
boot module (`composition/repl.py`, `api.py`, `a2a.py`,
`mcp_server.py`) calls this and then runs its own extras pipeline to
add plugins, vector memory, hooks, etc.

What core does:
- env bootstrap (idempotent — `bootstrap_env()`)
- ObscuraClient instantiation with surface-specific knobs
- await client.start() (which connects MCP servers if any)

What core does NOT do (extras blocks add):
- plugin tool registration
- system tool registration (system/browser/lsp/...)
- vector memory init
- hook loading
- supervisor / KAIROS / browser bridge / iMessage daemon
- system prompt enrichment beyond what the caller provides

This separation is the load-bearing principle: surface-specific
features stay in surface-specific extras pipelines, never in core.
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

    Per-surface boot modules call this, then run their extras pipeline.
    The returned session has only system-level wiring; plugin/hook/
    vector blocks must be installed by the caller.

    Parameters
    ----------
    config:
        Frozen session config (backend, model, prompt, mcp_servers, etc.)
    surface:
        Which entry point built me. Stored on `session.surface` and
        used by tools that legitimately want to know.
    user:
        Authenticated user (None for REPL local sessions).
    host_callbacks:
        Surface-supplied callbacks (ask_user, permission_mode, etc.)
        threaded into the client and exposed on the session.
    auth:
        Optional `AuthConfig` for backends that take one (e.g. Copilot
        with a forwarded OAuth token).
    session_id:
        Optional fixed session id (for resume / cross-surface
        correlation). Defaults to a fresh uuid.
    """
    # 1. .env load — idempotent
    from obscura.cli._env_loader import bootstrap_env

    bootstrap_env()

    # 2. Build the underlying client
    from obscura.core.client import ObscuraClient

    sid = session_id or new_session_id()
    callbacks = host_callbacks or {}

    client = ObscuraClient(
        config.backend,
        model=config.model,
        system_prompt=config.system_prompt,
        tools=preregistered_tools or None,
        mcp_servers=config.mcp_servers or None,
        user=user,
        auth=auth,
        host_callbacks=callbacks,
        hooks=hooks,
        # Composition surfaces install skill context via the
        # install_skill_context block AFTER core build (so
        # capability_resolver from install_plugin_tools is available).
        # ObscuraClient's own inject path stays for non-composition
        # callers (Agent.start, direct SDK use).
        inject_claude_context=False,
    )

    # 3. Start the client (connects MCP servers, prepares backend)
    try:
        await client.start()
    except Exception:
        logger.exception("build_core_session: client start failed")
        raise

    session = AgentSession(
        session_id=sid,
        surface=surface,
        config=config,
        client=client,
        host_callbacks=dict(callbacks),
        system_prompt=config.system_prompt,
    )
    # Mirror reliability state from the freshly-built client so the
    # session's own send/stream/run_loop methods can use them directly
    # (Stage 3 of ObscuraClient absorption — session methods stop
    # forwarding through client.X).
    session._capability_token = getattr(client, "_capability_token", None)
    session._circuit_registry = getattr(client, "_circuit_registry", None)
    session._cache = getattr(client, "_cache", None)
    session._max_retries = getattr(client, "_max_retries", 2)
    session._retry_initial_backoff = getattr(
        client, "_retry_initial_backoff", 0.5,
    )
    return session

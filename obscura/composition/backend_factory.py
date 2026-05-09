"""obscura.composition.backend_factory — backend instantiation, surface-agnostic.

Dispatch to the right backend (Copilot, Claude, OpenAI, Codex, Moonshot,
LocalLLM) wrapped in the throttle gate. Extracted from
``ObscuraClient._create_backend`` so the composition layer can build
backends without going through ObscuraClient — step in the
ObscuraClient absorption.

ObscuraClient still calls ``create_backend`` from this module, so
existing callers keep working unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from obscura.core.enums.agent import Backend
from obscura.core.throttle import wrap_if_enabled
from obscura.providers.claude import ClaudeBackend
from obscura.providers.codex import CodexBackend
from obscura.providers.copilot import CopilotBackend
from obscura.providers.localllm import LocalLLMBackend
from obscura.providers.moonshot import MoonshotBackend
from obscura.providers.openai import OpenAIBackend

if TYPE_CHECKING:
    from obscura.core.auth import AuthConfig
    from obscura.core.tool_policy import ToolPolicy
    from obscura.core.types import BackendProtocol


def create_backend(
    backend: Backend,
    auth: AuthConfig,
    model: str | None,
    system_prompt: str,
    mcp_servers: list[dict[str, Any]] | None,
    permission_mode: str = "default",
    cwd: str | None = None,
    streaming: bool = True,
    tool_policy: ToolPolicy | None = None,
) -> BackendProtocol:
    """Instantiate the backend for ``backend``, wrapped in the throttle gate.

    Returns the backend ready for ``await backend.start()`` (the caller
    is responsible for lifecycle).

    Raises:
        ValueError: backend is not a recognised member of ``Backend``.
    """
    instance: BackendProtocol
    backend_name: str

    if backend == Backend.COPILOT:
        instance = CopilotBackend(
            auth=auth,
            model=model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            streaming=streaming,
            tool_policy=tool_policy,
        )
        backend_name = "copilot"
    elif backend == Backend.CLAUDE:
        instance = ClaudeBackend(
            auth=auth,
            model=model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            permission_mode=permission_mode,
            cwd=cwd,
            tool_policy=tool_policy,
        )
        backend_name = "claude"
    elif backend == Backend.LOCALLLM:
        instance = LocalLLMBackend(
            auth=auth,
            model=model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
        )
        backend_name = "localllm"
    elif backend == Backend.OPENAI:
        instance = OpenAIBackend(
            auth=auth,
            model=model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
        )
        backend_name = "openai"
    elif backend == Backend.CODEX:
        instance = CodexBackend(
            auth=auth,
            model=model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
        )
        backend_name = "codex"
    elif backend == Backend.MOONSHOT:
        instance = MoonshotBackend(
            auth=auth,
            model=model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
        )
        backend_name = "moonshot"
    else:
        msg = f"Unknown backend: {backend}"
        raise ValueError(msg)

    return wrap_if_enabled(instance, backend_name=backend_name, auth=auth)

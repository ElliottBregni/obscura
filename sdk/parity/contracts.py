"""Method contracts for backend behavioral parity."""

from __future__ import annotations

from sdk.internal.types import Backend
from sdk.parity.models import MethodContract


CONTRACTS: tuple[MethodContract, ...] = (
    MethodContract(
        id="core.lifecycle",
        title="Backend lifecycle methods",
        required_methods=("start", "stop"),
    ),
    MethodContract(
        id="core.messaging",
        title="Message send and stream methods",
        required_methods=("send", "stream"),
        required_capabilities=("supports_streaming",),
    ),
    MethodContract(
        id="core.sessions.methods",
        title="Session method surface",
        required_methods=(
            "create_session",
            "resume_session",
            "list_sessions",
            "delete_session",
        ),
    ),
    MethodContract(
        id="core.tools",
        title="Tool registration and registry methods",
        required_methods=("register_tool", "get_tool_registry"),
        required_capabilities=("supports_tool_calls",),
    ),
    MethodContract(
        id="core.hooks",
        title="Hook registration method",
        required_methods=("register_hook",),
    ),
    MethodContract(
        id="core.native",
        title="Native handle + native mode capability",
        required_methods=("native",),
        required_capabilities=("supports_native_mode",),
        required_native_features=("native_client",),
    ),
    MethodContract(
        id="core.loop",
        title="Agent loop surface",
        required_methods=("run_loop",),
    ),
    MethodContract(
        id="claude.permission_modes",
        title="Claude permission mode native feature",
        required_native_features=("permission_modes",),
        applicable_backends=(Backend.CLAUDE,),
    ),
    MethodContract(
        id="claude.copilot.remote_sessions",
        title="Remote session capability for hosted backends",
        required_capabilities=("supports_remote_sessions",),
        applicable_backends=(Backend.CLAUDE, Backend.COPILOT),
    ),
    MethodContract(
        id="openai.responses_api",
        title="OpenAI responses native feature",
        required_native_features=("responses_api",),
        applicable_backends=(Backend.OPENAI, Backend.MOONSHOT),
    ),
    MethodContract(
        id="copilot.event_stream",
        title="Copilot event stream native feature",
        required_native_features=("event_stream",),
        applicable_backends=(Backend.COPILOT,),
    ),
    MethodContract(
        id="localllm.health_check",
        title="LocalLLM health-check native feature",
        required_native_features=("health_check",),
        applicable_backends=(Backend.LOCALLLM,),
    ),
)

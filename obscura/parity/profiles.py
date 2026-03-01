"""Backend feature support profiles used for parity scoring."""

from __future__ import annotations

from obscura.core.types import Backend
from obscura.parity.models import BackendParityProfile, FeatureStatus, FeatureSupport


PROFILES: tuple[BackendParityProfile, ...] = (
    BackendParityProfile(
        backend=Backend.OPENAI,
        supports=(
            FeatureSupport("session_create", FeatureStatus.SUPPORTED),
            FeatureSupport("session_resume", FeatureStatus.SUPPORTED),
            FeatureSupport(
                "session_fork",
                FeatureStatus.SUPPORTED,
                "Logical fork fallback clones session history.",
            ),
            FeatureSupport("stream_text", FeatureStatus.SUPPORTED),
            FeatureSupport(
                "stream_thinking", FeatureStatus.PARTIAL, "Model dependent."
            ),
            FeatureSupport("stream_tool_lifecycle", FeatureStatus.SUPPORTED),
            FeatureSupport("native_event_passthrough", FeatureStatus.SUPPORTED),
            FeatureSupport("tool_choice", FeatureStatus.SUPPORTED),
            FeatureSupport("hooks", FeatureStatus.SUPPORTED),
            FeatureSupport("openai_responses", FeatureStatus.SUPPORTED),
        ),
    ),
    BackendParityProfile(
        backend=Backend.MOONSHOT,
        supports=(
            FeatureSupport("session_create", FeatureStatus.SUPPORTED),
            FeatureSupport("session_resume", FeatureStatus.SUPPORTED),
            FeatureSupport(
                "session_fork",
                FeatureStatus.SUPPORTED,
                "Logical fork fallback clones session history.",
            ),
            FeatureSupport("stream_text", FeatureStatus.SUPPORTED),
            FeatureSupport(
                "stream_thinking", FeatureStatus.PARTIAL, "Model dependent."
            ),
            FeatureSupport("stream_tool_lifecycle", FeatureStatus.SUPPORTED),
            FeatureSupport("native_event_passthrough", FeatureStatus.SUPPORTED),
            FeatureSupport("tool_choice", FeatureStatus.SUPPORTED),
            FeatureSupport("hooks", FeatureStatus.PARTIAL, "Unified hook points only."),
            FeatureSupport("openai_responses", FeatureStatus.SUPPORTED),
        ),
    ),
    BackendParityProfile(
        backend=Backend.CLAUDE,
        supports=(
            FeatureSupport("session_create", FeatureStatus.SUPPORTED),
            FeatureSupport("session_resume", FeatureStatus.SUPPORTED),
            FeatureSupport("session_fork", FeatureStatus.SUPPORTED),
            FeatureSupport("stream_text", FeatureStatus.SUPPORTED),
            FeatureSupport("stream_thinking", FeatureStatus.SUPPORTED),
            FeatureSupport("stream_tool_lifecycle", FeatureStatus.SUPPORTED),
            FeatureSupport("native_event_passthrough", FeatureStatus.SUPPORTED),
            FeatureSupport("tool_choice", FeatureStatus.SUPPORTED),
            FeatureSupport("hooks", FeatureStatus.SUPPORTED),
            FeatureSupport("claude_permission_modes", FeatureStatus.SUPPORTED),
        ),
    ),
    BackendParityProfile(
        backend=Backend.COPILOT,
        supports=(
            FeatureSupport("session_create", FeatureStatus.SUPPORTED),
            FeatureSupport("session_resume", FeatureStatus.SUPPORTED),
            FeatureSupport(
                "session_fork",
                FeatureStatus.SUPPORTED,
                "Uses SDK fork when available, otherwise logical fork fallback.",
            ),
            FeatureSupport("stream_text", FeatureStatus.SUPPORTED),
            FeatureSupport("stream_thinking", FeatureStatus.SUPPORTED),
            FeatureSupport("stream_tool_lifecycle", FeatureStatus.SUPPORTED),
            FeatureSupport("native_event_passthrough", FeatureStatus.SUPPORTED),
            FeatureSupport("tool_choice", FeatureStatus.SUPPORTED),
            FeatureSupport("hooks", FeatureStatus.SUPPORTED),
            FeatureSupport("copilot_event_stream", FeatureStatus.SUPPORTED),
        ),
    ),
    BackendParityProfile(
        backend=Backend.LOCALLLM,
        supports=(
            FeatureSupport("session_create", FeatureStatus.SUPPORTED),
            FeatureSupport("session_resume", FeatureStatus.SUPPORTED),
            FeatureSupport(
                "session_fork",
                FeatureStatus.SUPPORTED,
                "Logical fork fallback clones session history.",
            ),
            FeatureSupport("stream_text", FeatureStatus.SUPPORTED),
            FeatureSupport(
                "stream_thinking", FeatureStatus.PARTIAL, "Depends on local server."
            ),
            FeatureSupport(
                "stream_tool_lifecycle",
                FeatureStatus.PARTIAL,
                "Depends on server tool support.",
            ),
            FeatureSupport("native_event_passthrough", FeatureStatus.SUPPORTED),
            FeatureSupport("tool_choice", FeatureStatus.SUPPORTED),
            FeatureSupport("hooks", FeatureStatus.SUPPORTED),
            FeatureSupport("localllm_health_models", FeatureStatus.SUPPORTED),
        ),
    ),
)


def profile_map() -> dict[Backend, BackendParityProfile]:
    """Index backend profiles by backend enum."""
    return {p.backend: p for p in PROFILES}

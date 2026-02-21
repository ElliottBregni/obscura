"""Feature registry for semantic parity scoring."""

from __future__ import annotations

from sdk.parity.models import ParityFeature

FEATURES: tuple[ParityFeature, ...] = (
    ParityFeature("session_create", "Session Create", "Create provider-backed session."),
    ParityFeature("session_resume", "Session Resume", "Resume an existing session."),
    ParityFeature("session_fork", "Session Fork", "Fork/branch session where supported.", weight=1.5),
    ParityFeature("stream_text", "Stream Text", "Emit text deltas in stream lifecycle."),
    ParityFeature("stream_thinking", "Stream Thinking", "Emit reasoning/thinking deltas."),
    ParityFeature("stream_tool_lifecycle", "Stream Tool Lifecycle", "Emit tool start/delta/end lifecycle."),
    ParityFeature("native_event_passthrough", "Native Event Passthrough", "Expose raw provider events."),
    ParityFeature("tool_choice", "Tool Choice", "Support tool choice policy for requests."),
    ParityFeature("hooks", "Hooks", "Support provider hook registration and callback flow."),
    ParityFeature("openai_responses", "OpenAI Responses", "Support native Responses API lane."),
    ParityFeature("claude_permission_modes", "Claude Permission Modes", "Support native Claude permission modes."),
    ParityFeature("copilot_event_stream", "Copilot Event Stream", "Support Copilot SDK event stream semantics."),
    ParityFeature("localllm_health_models", "LocalLLM Health + Models", "Support model list and health checks."),
)


def feature_map() -> dict[str, ParityFeature]:
    """Index registry by feature id."""
    return {f.id: f for f in FEATURES}

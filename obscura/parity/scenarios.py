"""Declarative scenario definitions for parity runner."""

from __future__ import annotations

from obscura.core.types import Backend
from obscura.parity.models import ScenarioExpectation, ScenarioSpec


SCENARIOS: tuple[tuple[ScenarioSpec, ScenarioExpectation], ...] = (
    (
        ScenarioSpec(
            id="openai.responses.native_send",
            title="OpenAI native responses send",
            feature_ids=("openai_responses", "native_event_passthrough"),
            backend=Backend.OPENAI,
        ),
        ScenarioExpectation(should_pass=True, expected_events=("response.completed",)),
    ),
    (
        ScenarioSpec(
            id="claude.permission_mode.plan",
            title="Claude permission mode plan",
            feature_ids=("claude_permission_modes",),
            backend=Backend.CLAUDE,
        ),
        ScenarioExpectation(should_pass=True),
    ),
    (
        ScenarioSpec(
            id="copilot.stream.lifecycle",
            title="Copilot event stream lifecycle",
            feature_ids=("copilot_event_stream", "stream_tool_lifecycle"),
            backend=Backend.COPILOT,
        ),
        ScenarioExpectation(
            should_pass=True,
            expected_events=("assistant.message_delta", "tool.execution_start", "session.idle"),
        ),
    ),
    (
        ScenarioSpec(
            id="localllm.health.models",
            title="LocalLLM health and model discovery",
            feature_ids=("localllm_health_models",),
            backend=Backend.LOCALLLM,
        ),
        ScenarioExpectation(should_pass=True),
    ),
)

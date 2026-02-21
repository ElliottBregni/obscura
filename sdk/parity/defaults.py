"""Default parity conformance execution helpers."""

from __future__ import annotations

from sdk.backends.claude import ClaudeBackend
from sdk.backends.copilot import CopilotBackend
from sdk.backends.localllm import LocalLLMBackend
from sdk.backends.moonshot import MoonshotBackend
from sdk.backends.openai_compat import OpenAIBackend
from sdk.internal.auth import AuthConfig
from sdk.parity.conformance import evaluate_backend_conformance
from sdk.parity.contracts import CONTRACTS
from sdk.parity.models import BackendConformance


def default_backend_conformance() -> tuple[BackendConformance, ...]:
    """Evaluate method contracts across all built-in backends."""
    backends = (
        OpenAIBackend(AuthConfig(openai_api_key="sk-test")),
        MoonshotBackend(AuthConfig(moonshot_api_key="msk-test")),
        ClaudeBackend(AuthConfig(anthropic_api_key="ak-test")),
        CopilotBackend(AuthConfig(github_token="gh-test")),
        LocalLLMBackend(AuthConfig(localllm_base_url="http://localhost:1234/v1")),
    )
    return tuple(evaluate_backend_conformance(backend, CONTRACTS) for backend in backends)

"""Default parity conformance execution helpers."""

from __future__ import annotations

from obscura.providers.claude import ClaudeBackend
from obscura.providers.copilot import CopilotBackend
from obscura.providers.localllm import LocalLLMBackend
from obscura.providers.moonshot import MoonshotBackend
from obscura.providers.openai import OpenAIBackend
from obscura.core.auth import AuthConfig
from obscura.parity.conformance import evaluate_backend_conformance
from obscura.parity.contracts import CONTRACTS
from obscura.parity.models import BackendConformance


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

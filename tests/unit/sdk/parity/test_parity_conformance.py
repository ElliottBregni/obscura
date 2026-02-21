from __future__ import annotations

from sdk.backends.claude import ClaudeBackend
from sdk.backends.copilot import CopilotBackend
from sdk.backends.localllm import LocalLLMBackend
from sdk.backends.moonshot import MoonshotBackend
from sdk.backends.openai_compat import OpenAIBackend
from sdk.internal.auth import AuthConfig
from sdk.internal.types import Backend
from sdk.parity.conformance import evaluate_backend_conformance
from sdk.parity.contracts import CONTRACTS


def _all_backends() -> list[tuple[Backend, object]]:
    return [
        (Backend.OPENAI, OpenAIBackend(AuthConfig(openai_api_key="sk-test"))),
        (Backend.MOONSHOT, MoonshotBackend(AuthConfig(moonshot_api_key="msk-test"))),
        (Backend.CLAUDE, ClaudeBackend(AuthConfig(anthropic_api_key="ak-test"))),
        (Backend.COPILOT, CopilotBackend(AuthConfig(github_token="gh-test"))),
        (
            Backend.LOCALLLM,
            LocalLLMBackend(
                AuthConfig(localllm_base_url="http://localhost:1234/v1"),
            ),
        ),
    ]


def test_conformance_all_backends_contracts_pass() -> None:
    for backend_enum, backend_impl in _all_backends():
        result = evaluate_backend_conformance(backend_impl, CONTRACTS)
        assert result.backend is backend_enum
        assert result.total > 0
        assert result.passed == result.total
        assert result.percent == 100.0


def test_conformance_contract_subset_is_backend_specific() -> None:
    openai = OpenAIBackend(AuthConfig(openai_api_key="sk-test"))
    result = evaluate_backend_conformance(openai, CONTRACTS)
    ids = {c.contract_id for c in result.checks}
    assert "openai.responses_api" in ids
    moonshot = MoonshotBackend(AuthConfig(moonshot_api_key="msk-test"))
    moonshot_result = evaluate_backend_conformance(moonshot, CONTRACTS)
    moonshot_ids = {c.contract_id for c in moonshot_result.checks}
    assert "openai.responses_api" in moonshot_ids
    assert "claude.permission_modes" not in ids
    assert "copilot.event_stream" not in ids
    assert "localllm.health_check" not in ids

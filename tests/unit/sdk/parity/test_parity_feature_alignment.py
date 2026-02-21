from __future__ import annotations

from sdk.backends.claude import ClaudeBackend
from sdk.backends.copilot import CopilotBackend
from sdk.backends.localllm import LocalLLMBackend
from sdk.backends.moonshot import MoonshotBackend
from sdk.backends.openai_compat import OpenAIBackend
from sdk.internal.auth import AuthConfig
from sdk.internal.types import Backend
from sdk.parity.profiles import profile_map


def test_profiles_align_with_backends() -> None:
    profiles = profile_map()

    openai_caps = OpenAIBackend(AuthConfig(openai_api_key="sk-test")).capabilities()
    claude_caps = ClaudeBackend(AuthConfig(anthropic_api_key="sk-ant-test")).capabilities()
    copilot_caps = CopilotBackend(AuthConfig(github_token="gh-test")).capabilities()
    local_caps = LocalLLMBackend(
        AuthConfig(localllm_base_url="http://localhost:1234/v1")
    ).capabilities()
    moonshot_caps = MoonshotBackend(AuthConfig(moonshot_api_key="msk-test")).capabilities()

    assert Backend.OPENAI in profiles
    assert Backend.MOONSHOT in profiles
    assert Backend.CLAUDE in profiles
    assert Backend.COPILOT in profiles
    assert Backend.LOCALLLM in profiles

    assert openai_caps.supports_native_mode
    assert claude_caps.supports_native_mode
    assert copilot_caps.supports_native_mode
    assert local_caps.supports_native_mode
    assert moonshot_caps.supports_native_mode

    assert "responses_api" in openai_caps.native_features
    assert "session_fork" in claude_caps.native_features
    assert "event_stream" in copilot_caps.native_features
    assert "health_check" in local_caps.native_features
    assert "responses_api" in moonshot_caps.native_features

"""Tests for sdk.internal.auth — Auth resolution and TokenRefresher."""

import pytest
import os
from unittest.mock import patch, MagicMock

from obscura.core.auth import (
    AuthConfig,
    resolve_auth,
    resolve_github_token,
    resolve_anthropic_key,
    resolve_openai_key,
    resolve_openai_base_url,
    resolve_localllm_base_url,
    TokenRefresher,
)
from obscura.core.types import Backend


class TestAuthConfig:
    def test_defaults(self):
        config = AuthConfig()
        assert config.github_token is None
        assert config.anthropic_api_key is None
        assert config.openai_api_key is None
        assert config.openai_base_url is None
        assert config.localllm_base_url is None

    def test_frozen(self):
        config = AuthConfig(github_token="tok")
        with pytest.raises(Exception):
            config.github_token = "other"


class TestResolveGithubToken:
    def test_explicit(self):
        assert resolve_github_token("my-token") == "my-token"

    def test_env_var(self):
        with patch.dict(
            os.environ, {"GH_TOKEN": "env-token", "COPILOT_API_KEY": ""}, clear=False
        ):
            cli_result = MagicMock()
            cli_result.returncode = 1
            cli_result.stdout = ""
            with patch("subprocess.run", return_value=cli_result):
                assert resolve_github_token(None) == "env-token"

    def test_oauth_first_over_env(self):
        with patch.dict(os.environ, {"GH_TOKEN": "env-token"}, clear=True):
            cli_result = MagicMock()
            cli_result.returncode = 0
            cli_result.stdout = "oauth-token\n"
            with patch("subprocess.run", return_value=cli_result):
                assert resolve_github_token(None) == "oauth-token"

    def test_env_first_over_oauth(self):
        with patch.dict(
            os.environ,
            {"GH_TOKEN": "env-token", "OBSCURA_AUTH_MODE": "env_first"},
            clear=True,
        ):
            cli_result = MagicMock()
            cli_result.returncode = 0
            cli_result.stdout = "oauth-token\n"
            with patch("subprocess.run", return_value=cli_result):
                assert resolve_github_token(None) == "env-token"

    def test_gh_cli_fallback(self):
        env = {
            k: ""
            for k in (
                "COPILOT_API_KEY",
                "COPILOT_GITHUB_TOKEN",
                "GH_TOKEN",
                "GITHUB_TOKEN",
            )
        }
        with patch.dict(os.environ, env, clear=False):
            # Clear all env vars
            for k in env:
                os.environ.pop(k, None)
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "cli-token\n"
            with patch("subprocess.run", return_value=mock_result):
                assert resolve_github_token(None) == "cli-token"

    def test_gh_cli_not_found(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                with pytest.raises(ValueError, match="Copilot auth"):
                    resolve_github_token(None)

    def test_gh_cli_timeout(self):
        with patch.dict(os.environ, {}, clear=True):
            import subprocess

            with patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 5)
            ):
                with pytest.raises(ValueError, match="Copilot auth"):
                    resolve_github_token(None)

    def test_cmd_fallback(self):
        with patch.dict(
            os.environ,
            {"OBSCURA_GITHUB_TOKEN_CMD": "echo gh-cmd-token"},
            clear=True,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "gh-cmd-token\n"
            with patch("subprocess.run", return_value=mock_result):
                assert resolve_github_token(None) == "gh-cmd-token"

    def test_env_first_prefers_env_over_cmd(self):
        with patch.dict(
            os.environ,
            {
                "GH_TOKEN": "env-token",
                "OBSCURA_GITHUB_TOKEN_CMD": "echo gh-cmd-token",
                "OBSCURA_AUTH_MODE": "env_first",
            },
            clear=True,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "gh-cmd-token\n"
            with patch("subprocess.run", return_value=mock_result):
                assert resolve_github_token(None) == "env-token"


class TestResolveAnthropicKey:
    def test_explicit(self):
        assert resolve_anthropic_key("key-123") == "key-123"

    def test_env_var(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}):
            assert resolve_anthropic_key(None) == "env-key"

    def test_alt_env_var(self):
        with patch.dict(os.environ, {"CLAUDE_API_KEY": "alt-env-key"}, clear=True):
            assert resolve_anthropic_key(None) == "alt-env-key"

    def test_cmd_fallback(self):
        with patch.dict(
            os.environ,
            {"OBSCURA_CLAUDE_TOKEN_CMD": "echo cmd-key"},
            clear=True,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "cmd-key\n"
            with patch("subprocess.run", return_value=mock_result):
                assert resolve_anthropic_key(None) == "cmd-key"

    def test_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="Claude auth"):
                resolve_anthropic_key(None)


class TestResolveOpenAIKey:
    def test_explicit(self):
        assert resolve_openai_key("oai-key") == "oai-key"

    def test_env_var(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-oai"}):
            status = MagicMock()
            status.returncode = 1
            status.stdout = ""
            status.stderr = ""
            with patch("subprocess.run", return_value=status):
                assert resolve_openai_key(None) == "env-oai"

    def test_alt_env_var_codex(self):
        with patch.dict(os.environ, {"CODEX_API_KEY": "codex-env-key"}, clear=True):
            assert resolve_openai_key(None) == "codex-env-key"

    def test_cmd_fallback(self):
        with patch.dict(
            os.environ,
            {"OBSCURA_OPENAI_TOKEN_CMD": "echo openai-cmd-key"},
            clear=True,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "openai-cmd-key\n"
            with patch("subprocess.run", return_value=mock_result):
                assert resolve_openai_key(None) == "openai-cmd-key"

    def test_codex_oauth_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            status = MagicMock()
            status.returncode = 0
            status.stdout = ""
            status.stderr = "Logged in using ChatGPT\n"
            with patch("subprocess.run", return_value=status):
                fake_payload = '{"tokens": {"access_token": "codex-oauth-token"}}'
                with patch("pathlib.Path.read_text", return_value=fake_payload):
                    assert resolve_openai_key(None) == "codex-oauth-token"

    def test_codex_oauth_first_over_env(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-oai"}, clear=True):
            status = MagicMock()
            status.returncode = 0
            status.stdout = ""
            status.stderr = "Logged in using ChatGPT\n"
            with patch("subprocess.run", return_value=status):
                fake_payload = '{"tokens": {"access_token": "codex-oauth-token"}}'
                with patch("pathlib.Path.read_text", return_value=fake_payload):
                    assert resolve_openai_key(None) == "codex-oauth-token"

    def test_env_first_over_codex_oauth(self):
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "env-oai", "OBSCURA_AUTH_MODE": "env_first"},
            clear=True,
        ):
            with patch("subprocess.run") as mock_run:
                assert resolve_openai_key(None) == "env-oai"
                mock_run.assert_not_called()

    def test_codex_oauth_logged_out(self):
        with patch.dict(os.environ, {}, clear=True):
            status = MagicMock()
            status.returncode = 0
            status.stdout = "Not logged in\n"
            with patch("subprocess.run", return_value=status):
                with pytest.raises(ValueError, match="OpenAI auth"):
                    resolve_openai_key(None)

    def test_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="OpenAI auth"):
                resolve_openai_key(None)


class TestResolveOpenAIBaseURL:
    def test_explicit(self):
        assert resolve_openai_base_url("http://custom") == "http://custom"

    def test_env_var(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://env-url"}):
            assert resolve_openai_base_url(None) == "http://env-url"

    def test_default_none(self):
        with patch.dict(os.environ, {}, clear=True):
            assert resolve_openai_base_url(None) is None


class TestResolveLocalLLMBaseURL:
    def test_explicit(self):
        assert resolve_localllm_base_url("http://my-llm") == "http://my-llm"

    def test_env_var(self):
        with patch.dict(os.environ, {"LOCALLLM_BASE_URL": "http://env-llm"}):
            assert resolve_localllm_base_url(None) == "http://env-llm"

    def test_default(self):
        with patch.dict(os.environ, {}, clear=True):
            assert resolve_localllm_base_url(None) == "http://localhost:1234/v1"


class TestResolveAuth:
    def test_copilot(self):
        config = AuthConfig(github_token="tok")
        result = resolve_auth(Backend.COPILOT, config)
        assert result.github_token == "tok"

    def test_copilot_byok(self):
        config = AuthConfig(byok_provider={"provider": "azure"})
        result = resolve_auth(Backend.COPILOT, config)
        assert result.byok_provider is not None

    def test_claude(self):
        config = AuthConfig(anthropic_api_key="key")
        result = resolve_auth(Backend.CLAUDE, config)
        assert result.anthropic_api_key == "key"

    def test_claude_oauth_fallback_when_not_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            status = MagicMock()
            status.returncode = 0
            status.stdout = '{"loggedIn": true}'
            with patch("subprocess.run", return_value=status):
                result = resolve_auth(Backend.CLAUDE)
                assert result.anthropic_api_key is None

    def test_claude_oauth_first_over_env(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}, clear=True):
            status = MagicMock()
            status.returncode = 0
            status.stdout = '{"loggedIn": true}'
            with patch("subprocess.run", return_value=status):
                result = resolve_auth(Backend.CLAUDE)
                assert result.anthropic_api_key is None

    def test_claude_env_first_over_oauth(self):
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "env-key", "OBSCURA_AUTH_MODE": "env_first"},
            clear=True,
        ):
            status = MagicMock()
            status.returncode = 0
            status.stdout = '{"loggedIn": true}'
            with patch("subprocess.run", return_value=status):
                result = resolve_auth(Backend.CLAUDE)
                assert result.anthropic_api_key == "env-key"

    def test_claude_env_first_oauth_late_fallback(self):
        with patch.dict(os.environ, {"OBSCURA_AUTH_MODE": "env_first"}, clear=True):
            status = MagicMock()
            status.returncode = 0
            status.stdout = '{"loggedIn": true}'
            with patch("subprocess.run", return_value=status):
                result = resolve_auth(Backend.CLAUDE)
                assert result.anthropic_api_key is None

    def test_claude_oauth_fallback_not_logged_in(self):
        with patch.dict(os.environ, {}, clear=True):
            status = MagicMock()
            status.returncode = 0
            status.stdout = '{"loggedIn": false}'
            with patch("subprocess.run", return_value=status):
                with pytest.raises(ValueError, match="Claude auth"):
                    resolve_auth(Backend.CLAUDE)

    def test_openai(self):
        config = AuthConfig(openai_api_key="key", openai_base_url="http://custom")
        result = resolve_auth(Backend.OPENAI, config)
        assert result.openai_api_key == "key"
        assert result.openai_base_url == "http://custom"

    def test_codex(self):
        config = AuthConfig(openai_api_key="key", openai_base_url="http://custom")
        result = resolve_auth(Backend.CODEX, config)
        assert result.openai_api_key == "key"
        assert result.openai_base_url == "http://custom"

    def test_codex_without_explicit_credentials_is_allowed(self):
        result = resolve_auth(Backend.CODEX, AuthConfig())
        assert result.openai_api_key is None

    def test_localllm(self):
        result = resolve_auth(Backend.LOCALLLM)
        assert result.localllm_base_url is not None

    def test_unknown_backend(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            resolve_auth(Backend.CLAUDE, AuthConfig(), None)  # wrong path not reachable


class TestTokenRefresher:
    @pytest.mark.asyncio
    async def test_get_valid_auth_cached(self):
        refresher = TokenRefresher(
            Backend.LOCALLLM,
            refresh_interval=3600,
        )
        auth1 = await refresher.get_valid_auth()
        auth2 = await refresher.get_valid_auth()
        assert auth1 is auth2  # Should be cached

    @pytest.mark.asyncio
    async def test_invalidate(self):
        refresher = TokenRefresher(Backend.LOCALLLM)
        await refresher.get_valid_auth()
        refresher.invalidate()
        assert refresher.cached_auth is None
        auth2 = await refresher.get_valid_auth()
        assert auth2 is not None

    @pytest.mark.asyncio
    async def test_refresh_on_expiry(self):
        refresher = TokenRefresher(
            Backend.LOCALLLM,
            refresh_interval=0,  # Always refresh
        )
        await refresher.get_valid_auth()
        auth2 = await refresher.get_valid_auth()
        # With interval=0, should re-resolve each time
        assert auth2 is not None

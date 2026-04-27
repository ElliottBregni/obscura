"""Tests for obscura.providers.codex.CodexBackend."""
# pyright: reportPrivateUsage=false, reportUnknownVariableType=false

from __future__ import annotations

from typing import Any

import pytest

from obscura.core.auth import AuthConfig
from obscura.core.types import Backend
from obscura.providers.codex import CodexBackend


def _auth(**kw: str | None) -> AuthConfig:
    return AuthConfig(openai_api_key=kw.get("api_key"))


class _FakeThread:
    def __init__(self, thread_id: str = "thr-1", text: str = "hello from sdk") -> None:
        self.id = thread_id
        self._text = text
        self.last_kwargs: dict[str, Any] = {}

    async def run(self, _prompt: str, **_kwargs: Any) -> Any:
        self.last_kwargs = dict(_kwargs)
        text = self._text

        class _RunResult:
            final_response = text
            items: list[Any] = []
            usage = None

        return _RunResult()


class _FakeCodex:
    """Mimic the subset of ``codex_app_server.AsyncCodex`` we depend on."""

    def __init__(self) -> None:
        self._thread = _FakeThread()
        self.started = False

    async def __aenter__(self) -> _FakeCodex:
        self.started = True
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.started = False

    async def thread_start(self, **_kwargs: Any) -> _FakeThread:
        return self._thread

    async def thread_resume(self, _thread_id: str, **_kwargs: Any) -> _FakeThread:
        return self._thread


class TestCodexBackend:
    @pytest.mark.asyncio
    async def test_start_requires_sdk(self) -> None:
        backend = CodexBackend(_auth())
        backend._import_sdk_class = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("Official OpenAI Codex SDK not found"),
        )
        with pytest.raises(RuntimeError, match="Official OpenAI Codex SDK not found"):
            await backend.start()

    @pytest.mark.asyncio
    async def test_sessions(self) -> None:
        backend = CodexBackend(_auth())
        backend._import_sdk_class = lambda: (_FakeCodex, "json")  # type: ignore[method-assign]
        await backend.start()
        ref = await backend.create_session()
        assert ref.backend is Backend.CODEX
        refs = await backend.list_sessions()
        assert any(r.session_id == ref.session_id for r in refs)
        await backend.resume_session(ref)
        await backend.delete_session(ref)

    @pytest.mark.asyncio
    async def test_concurrent_streams_serialize(self) -> None:
        """Two concurrent stream() callers must not both call thread.turn().

        Regression for: "Concurrent turn consumers are not yet supported in
        the experimental SDK." The REPL allows the user to type a new prompt
        while a turn is still streaming, so we serialize at the backend.
        """
        import asyncio

        from obscura.core.types import ChunkKind

        active_turns = 0
        max_active = 0
        turn_started = asyncio.Event()
        release_first_turn = asyncio.Event()

        class _GatedTurnHandle:
            def __init__(self, *, gate: bool) -> None:
                self._gate = gate

            async def stream(self):  # noqa: ANN202
                nonlocal active_turns, max_active
                active_turns += 1
                max_active = max(max_active, active_turns)
                try:
                    if self._gate:
                        turn_started.set()
                        await release_first_turn.wait()
                    return
                    yield  # pragma: no cover — make this an async generator
                finally:
                    active_turns -= 1

        class _GatedThread:
            def __init__(self) -> None:
                self.id = "thr-gated"
                self._calls = 0

            async def turn(self, _input: Any, **_kwargs: Any) -> Any:
                self._calls += 1
                # First caller is gated; second caller is not — but it
                # should be queued behind the lock and only reach here
                # once the first turn's stream is exhausted.
                return _GatedTurnHandle(gate=self._calls == 1)

        class _GatedCodex:
            def __init__(self) -> None:
                self._thread = _GatedThread()

            async def __aenter__(self) -> _GatedCodex:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def thread_start(self, **_kwargs: Any) -> _GatedThread:
                return self._thread

            async def thread_resume(
                self,
                _thread_id: str,
                **_kwargs: Any,
            ) -> _GatedThread:
                return self._thread

        backend = CodexBackend(_auth())
        backend._import_sdk_class = lambda: (_GatedCodex, "json")  # type: ignore[method-assign]
        await backend.start()

        async def _drain(prompt: str) -> list[str]:
            kinds: list[str] = []
            async for chunk in backend.stream(prompt):
                kinds.append(chunk.kind.name)
                if chunk.kind is ChunkKind.DONE:
                    break
            return kinds

        first = asyncio.create_task(_drain("first"))
        # Wait until the first turn is actually streaming.
        await asyncio.wait_for(turn_started.wait(), timeout=1.0)

        # Kick off the second turn. It must NOT call thread.turn() yet.
        second = asyncio.create_task(_drain("second"))
        await asyncio.sleep(0.05)
        assert max_active == 1, (
            "second stream() entered thread.turn() while first was active"
        )

        # Release the first turn; the second should now proceed.
        release_first_turn.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)
        assert max_active == 1


class TestMcpServerConfigOverrides:
    """Unit tests for the helper that maps Obscura's mcp_servers format
    onto Codex's ``-c mcp_servers.<name>.<field>=<toml>`` overrides."""

    def test_empty_list_returns_empty(self) -> None:
        from obscura.providers.codex import _mcp_servers_to_config_overrides

        assert _mcp_servers_to_config_overrides([]) == ()

    def test_http_server_emits_url_override(self) -> None:
        from obscura.providers.codex import _mcp_servers_to_config_overrides

        overrides = _mcp_servers_to_config_overrides(
            [{"name": "obscura-browser", "url": "http://127.0.0.1:50123/mcp"}],
        )
        assert overrides == (
            'mcp_servers.obscura_browser.url="http://127.0.0.1:50123/mcp"',
        )

    def test_http_server_with_bearer_token_env(self) -> None:
        from obscura.providers.codex import _mcp_servers_to_config_overrides

        overrides = _mcp_servers_to_config_overrides(
            [
                {
                    "name": "secure",
                    "url": "https://api.example.com/mcp",
                    "bearer_token_env_var": "API_TOKEN",
                },
            ],
        )
        assert 'mcp_servers.secure.url="https://api.example.com/mcp"' in overrides
        assert 'mcp_servers.secure.bearer_token_env_var="API_TOKEN"' in overrides

    def test_stdio_server_emits_command_args_env(self) -> None:
        from obscura.providers.codex import _mcp_servers_to_config_overrides

        overrides = _mcp_servers_to_config_overrides(
            [
                {
                    "name": "local",
                    "command": "mcp-filesystem",
                    "args": ["--root", "/tmp"],
                    "env": {"LOG_LEVEL": "info"},
                },
            ],
        )
        assert 'mcp_servers.local.command="mcp-filesystem"' in overrides
        assert 'mcp_servers.local.args=["--root", "/tmp"]' in overrides
        assert 'mcp_servers.local.env={ LOG_LEVEL = "info" }' in overrides

    def test_ignores_unnamed_entries(self) -> None:
        from obscura.providers.codex import _mcp_servers_to_config_overrides

        assert _mcp_servers_to_config_overrides([{"url": "http://x/"}]) == ()

    def test_string_values_are_toml_escaped(self) -> None:
        from obscura.providers.codex import _mcp_servers_to_config_overrides

        overrides = _mcp_servers_to_config_overrides(
            [{"name": "weird", "command": 'quote "me" and \\slash'}],
        )
        # Double quotes are backslash-escaped, backslashes are doubled.
        assert 'mcp_servers.weird.command="quote \\"me\\" and \\\\slash"' in overrides

    def test_name_sanitization_normalizes_to_alphanum_underscore(self) -> None:
        from obscura.providers.codex import _mcp_servers_to_config_overrides

        overrides = _mcp_servers_to_config_overrides(
            [{"name": "my name/with-weird chars", "url": "http://x/"}],
        )
        # Spaces, slashes, and dashes collapse to underscore for a
        # stable TOML dotted-path key.
        assert any(
            o.startswith("mcp_servers.my_name_with_weird_chars.url=") for o in overrides
        )


class TestBuildSdkClientForwardsMcpServers:
    """End-to-end: AppServerConfig receives our config_overrides."""

    @pytest.mark.asyncio
    async def test_mcp_servers_reach_app_server_config(self) -> None:
        captured: dict[str, Any] = {}

        class _FakeConfig:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        class _FakeCodexWithConfig:
            def __init__(self, config: Any) -> None:
                self.config = config

            async def __aenter__(self) -> _FakeCodexWithConfig:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        backend = CodexBackend(
            _auth(),
            mcp_servers=[
                {"name": "obscura-browser", "url": "http://127.0.0.1:8765/mcp"},
            ],
        )
        backend._import_sdk_class = lambda: (_FakeCodexWithConfig, "json")  # type: ignore[method-assign]
        # Install the SDK-symbol cache so _build_sdk_client picks up our stub.
        backend._sdk_syms = {"AppServerConfig": _FakeConfig}

        await backend.start()

        assert "config_overrides" in captured
        overrides = captured["config_overrides"]
        assert isinstance(overrides, tuple)
        assert any("mcp_servers.obscura_browser.url=" in o for o in overrides)

    @pytest.mark.asyncio
    async def test_no_overrides_when_mcp_servers_empty(self) -> None:
        captured: dict[str, Any] = {}

        class _FakeConfig:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        class _FakeCodexWithConfig:
            def __init__(self, config: Any) -> None:
                self.config = config

            async def __aenter__(self) -> _FakeCodexWithConfig:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        backend = CodexBackend(_auth())  # no mcp_servers
        backend._import_sdk_class = lambda: (_FakeCodexWithConfig, "json")  # type: ignore[method-assign]
        backend._sdk_syms = {"AppServerConfig": _FakeConfig}

        await backend.start()

        assert "config_overrides" not in captured

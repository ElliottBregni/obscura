"""Tests for obscura.providers.codex.CodexBackend."""

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

        class _Turn:
            final_response = self._text
            thread_id = "thr-1"

        return _Turn()


class _FakeCodex:
    def __init__(self) -> None:
        self._thread = _FakeThread()

    def start_thread(self) -> _FakeThread:
        return self._thread

    def resume_thread(self, _thread_id: str) -> _FakeThread:
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
    @pytest.mark.asyncio
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

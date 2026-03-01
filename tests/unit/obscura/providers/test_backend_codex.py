"""Tests for obscura.providers.codex.CodexBackend."""

from __future__ import annotations

from typing import Any

import pytest

from obscura.core.auth import AuthConfig
from obscura.core.types import Backend, ChunkKind
from obscura.providers.codex import CodexBackend


def _auth(**kw: str | None) -> AuthConfig:
    return AuthConfig(openai_api_key=kw.get("api_key"))


class _FakeThread:
    def __init__(self, thread_id: str = "thr-1", text: str = "hello from sdk") -> None:
        self.id = thread_id
        self._text = text

    async def run(self, _prompt: str, **_kwargs: Any) -> Any:
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
            RuntimeError("Codex Python SDK not found")
        )
        with pytest.raises(RuntimeError, match="Codex Python SDK not found"):
            await backend.start()

    @pytest.mark.asyncio
    async def test_send(self) -> None:
        backend = CodexBackend(_auth(api_key="sk-test"))
        backend._import_sdk_class = lambda: (_FakeCodex, "python_codex_sdk")  # type: ignore[method-assign]
        await backend.start()
        msg = await backend.send("hello")
        assert msg.backend is Backend.CODEX
        assert msg.text == "hello from sdk"
        await backend.stop()

    @pytest.mark.asyncio
    async def test_stream(self) -> None:
        backend = CodexBackend(_auth())
        backend._import_sdk_class = lambda: (_FakeCodex, "python_codex_sdk")  # type: ignore[method-assign]
        await backend.start()
        chunks: list[Any] = []
        async for c in backend.stream("x"):
            chunks.append(c)
        assert chunks[0].kind is ChunkKind.MESSAGE_START
        assert any(c.kind is ChunkKind.TEXT_DELTA for c in chunks)
        assert chunks[-1].kind is ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_sessions(self) -> None:
        backend = CodexBackend(_auth())
        backend._import_sdk_class = lambda: (_FakeCodex, "python_codex_sdk")  # type: ignore[method-assign]
        await backend.start()
        ref = await backend.create_session()
        assert ref.backend is Backend.CODEX
        refs = await backend.list_sessions()
        assert any(r.session_id == ref.session_id for r in refs)
        await backend.resume_session(ref)
        await backend.delete_session(ref)

    @pytest.mark.asyncio
    async def test_resume_thread_path(self) -> None:
        backend = CodexBackend(_auth())
        backend._import_sdk_class = lambda: (_FakeCodex, "python_codex_sdk")  # type: ignore[method-assign]
        await backend.start()
        ref = await backend.create_session()
        backend._thread_by_session[ref.session_id] = "thr-1"
        backend._thread_obj_by_id.clear()
        msg = await backend.send("resume")
        assert msg.text == "hello from sdk"

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
            RuntimeError("Official OpenAI Codex SDK not found")
        )
        with pytest.raises(RuntimeError, match="Official OpenAI Codex SDK not found"):
            await backend.start()

    @pytest.mark.asyncio
    async def test_send(self) -> None:
        backend = CodexBackend(_auth(api_key="sk-test"))
        backend._import_sdk_class = lambda: (_FakeCodex, "json")  # type: ignore[method-assign]
        await backend.start()
        msg = await backend.send("hello")
        assert msg.backend is Backend.CODEX
        assert msg.text == "hello from sdk"
        await backend.stop()

    @pytest.mark.asyncio
    async def test_stream(self) -> None:
        backend = CodexBackend(_auth())
        backend._import_sdk_class = lambda: (_FakeCodex, "json")  # type: ignore[method-assign]
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
        backend._import_sdk_class = lambda: (_FakeCodex, "json")  # type: ignore[method-assign]
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
        backend._import_sdk_class = lambda: (_FakeCodex, "json")  # type: ignore[method-assign]
        await backend.start()
        ref = await backend.create_session()
        backend._thread_by_session[ref.session_id] = "thr-1"
        backend._thread_obj_by_id.clear()
        msg = await backend.send("resume")
        assert msg.text == "hello from sdk"

    @pytest.mark.asyncio
    async def test_send_defaults_reasoning_effort_for_gpt_models(self) -> None:
        backend = CodexBackend(_auth(), model="gpt-5")
        backend._import_sdk_class = lambda: (_FakeCodex, "json")  # type: ignore[method-assign]
        await backend.start()
        await backend.send("hello")
        assert backend._sdk_client._thread.last_kwargs["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_send_respects_explicit_reasoning_effort(self) -> None:
        backend = CodexBackend(_auth(), model="gpt-5")
        backend._import_sdk_class = lambda: (_FakeCodex, "json")  # type: ignore[method-assign]
        await backend.start()
        await backend.send("hello", reasoning_effort="low")
        assert backend._sdk_client._thread.last_kwargs["reasoning_effort"] == "low"

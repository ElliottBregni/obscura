"""Tests for CopilotBackend session-expiry recovery (resume + replay).

When a Copilot session expires mid-turn (idle timeout, server-side state
gone), the backend used to silently create a fresh empty session and
retry the same call. For post-tool-call iterations the call has
``prompt=""`` and the empty session has no history, so the model
rationalized the empty turn as "user sent an empty message".

These tests pin down the recovery behavior:
1. ``_recover_session`` tries ``client.resume_session(old_id)`` first.
2. If resume fails, it falls back to a fresh session AND replays the
   prior conversation as a single context primer.
3. ``_build_context_primer`` produces a sensible flattened transcript.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from obscura.core.auth import AuthConfig
from obscura.core.types import ContentBlock, Message, Role
from obscura.providers.copilot import CopilotBackend


def _backend() -> CopilotBackend:
    return CopilotBackend(AuthConfig(github_token="gh-test"))


def _make_messages() -> list[Message]:
    return [
        Message(
            role=Role.USER,
            content=[ContentBlock(kind="text", text="What is the weather?")],
        ),
        Message(
            role=Role.ASSISTANT,
            content=[
                ContentBlock(kind="text", text="Looking it up."),
                ContentBlock(
                    kind="tool_use",
                    tool_name="get_weather",
                    tool_input={"city": "SF"},
                    tool_use_id="call_1",
                ),
            ],
        ),
        Message(
            role=Role.TOOL_RESULT,
            content=[
                ContentBlock(
                    kind="tool_result",
                    text="sunny, 68F",
                    tool_use_id="call_1",
                ),
            ],
        ),
    ]


class TestBuildContextPrimer:
    """Verify the priming-prompt construction is sensible."""

    def test_primer_includes_user_assistant_and_tool(self) -> None:
        primer = CopilotBackend._build_context_primer(_make_messages())
        assert "User: What is the weather?" in primer
        assert "Assistant: Looking it up." in primer
        assert "Assistant tool call: get_weather" in primer
        assert "Tool result: sunny, 68F" in primer

    def test_primer_starts_with_recovery_directive(self) -> None:
        primer = CopilotBackend._build_context_primer(_make_messages())
        # The model needs to know NOT to ask the user to repeat themselves.
        assert "recovered" in primer.lower()
        assert "do not ask the user to repeat" in primer.lower()
        assert "do not re-run" in primer.lower()

    def test_empty_messages_produce_only_directive(self) -> None:
        primer = CopilotBackend._build_context_primer([])
        # Still contains the directive header but no transcript lines.
        assert primer
        assert "User:" not in primer
        assert "Assistant:" not in primer


class TestRecoverySessionResume:
    """When resume_session succeeds, we don't recreate or re-prime."""

    async def test_resume_succeeds_no_fallback(self) -> None:
        backend = _backend()
        old_session = MagicMock()
        old_session.session_id = "old-sid-123"
        backend._session = old_session

        new_session = MagicMock()
        new_session.send = AsyncMock()
        client = MagicMock()
        client.resume_session = AsyncMock(return_value=new_session)
        client.create_session = AsyncMock()
        backend._client = client

        await backend._recover_session(prior_messages=_make_messages())

        client.resume_session.assert_awaited_once()
        # Resume succeeded — no fresh session and no primer send.
        client.create_session.assert_not_called()
        new_session.send.assert_not_called()
        assert backend._session is new_session

    async def test_resume_called_with_old_session_id(self) -> None:
        backend = _backend()
        old_session = MagicMock()
        old_session.session_id = "old-sid-abc"
        backend._session = old_session

        client = MagicMock()
        client.resume_session = AsyncMock(return_value=MagicMock())
        backend._client = client

        await backend._recover_session()

        args, _ = client.resume_session.call_args
        assert args[0] == "old-sid-abc"


class TestRecoveryFallbackReplay:
    """When resume_session fails, fall back to fresh-session + primer replay."""

    async def test_fallback_creates_fresh_session_and_primes(self) -> None:
        backend = _backend()
        old_session = MagicMock()
        old_session.session_id = "old-sid-456"
        backend._session = old_session

        fresh_session = MagicMock()
        fresh_session.send = AsyncMock()
        client = MagicMock()
        client.resume_session = AsyncMock(side_effect=RuntimeError("session not found"))
        client.create_session = AsyncMock(return_value=fresh_session)
        backend._client = client

        await backend._recover_session(prior_messages=_make_messages())

        client.resume_session.assert_awaited_once()
        client.create_session.assert_awaited_once()
        # Primer was sent to the fresh session.
        fresh_session.send.assert_awaited_once()
        sent_text = fresh_session.send.call_args.args[0]
        assert "User: What is the weather?" in sent_text
        assert "Tool result: sunny, 68F" in sent_text
        assert backend._session is fresh_session

    async def test_fallback_without_prior_messages_skips_primer(self) -> None:
        backend = _backend()
        old_session = MagicMock()
        old_session.session_id = "old-sid-789"
        backend._session = old_session

        fresh_session = MagicMock()
        fresh_session.send = AsyncMock()
        client = MagicMock()
        client.resume_session = AsyncMock(side_effect=RuntimeError("expired"))
        client.create_session = AsyncMock(return_value=fresh_session)
        backend._client = client

        await backend._recover_session(prior_messages=None)

        client.create_session.assert_awaited_once()
        # No prior messages — nothing to prime with.
        fresh_session.send.assert_not_called()

    async def test_no_old_session_id_skips_resume(self) -> None:
        """If we never had a session_id, jump straight to create_session."""
        backend = _backend()
        # _session is None or has no session_id attribute
        backend._session = None

        fresh_session = MagicMock()
        fresh_session.send = AsyncMock()
        client = MagicMock()
        client.resume_session = AsyncMock()
        client.create_session = AsyncMock(return_value=fresh_session)
        backend._client = client

        await backend._recover_session(prior_messages=None)

        client.resume_session.assert_not_called()
        client.create_session.assert_awaited_once()

    async def test_primer_send_failure_does_not_propagate(self) -> None:
        """A broken primer send must not abort recovery — log & move on."""
        backend = _backend()
        old_session = MagicMock()
        old_session.session_id = "old-sid"
        backend._session = old_session

        fresh_session = MagicMock()
        fresh_session.send = AsyncMock(side_effect=RuntimeError("network blip"))
        client = MagicMock()
        client.resume_session = AsyncMock(side_effect=RuntimeError("expired"))
        client.create_session = AsyncMock(return_value=fresh_session)
        backend._client = client

        # Should not raise — recovery is best-effort.
        await backend._recover_session(prior_messages=_make_messages())
        assert backend._session is fresh_session


class TestStreamPropagatesPriorMessages:
    """The stream() retry path must hand prior_messages to recovery."""

    async def test_stream_retry_passes_messages(self) -> None:
        backend = _backend()
        old_session = MagicMock()
        old_session.session_id = "old-sid"
        backend._session = old_session

        fresh_session = MagicMock()
        fresh_session.send = AsyncMock()
        client = MagicMock()
        client.resume_session = AsyncMock(side_effect=RuntimeError("expired"))
        client.create_session = AsyncMock(return_value=fresh_session)
        backend._client = client

        prior = _make_messages()
        # Patch _do_stream: first call raises session-expired, second yields nothing.
        call_count = {"n": 0}

        async def _do_stream_fake(prompt: str, **_: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("session not found")
            if False:
                yield  # pragma: no cover
            return

        backend._do_stream = _do_stream_fake  # type: ignore[assignment]

        # Drive the stream to completion (await both calls).
        chunks = [c async for c in backend.stream("", messages=prior)]
        assert chunks == []
        # Recovery used the fallback path with our prior messages.
        fresh_session.send.assert_awaited_once()
        sent_text = fresh_session.send.call_args.args[0]
        assert "User: What is the weather?" in sent_text

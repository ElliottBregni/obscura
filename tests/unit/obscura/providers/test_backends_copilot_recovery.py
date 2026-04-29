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


class TestEmptyPromptOnRecoveryOnly:
    """Healthy-session post-tool turns must pass ``prompt=""`` through
    verbatim — the live Copilot session has the tool_use + tool_result
    in server-side state and continues normally. Only the recovery
    retry path swaps in the internal cue, because the freshly-recovered
    session lacks that implicit "tool result waiting" context.
    """

    async def test_normal_empty_prompt_passes_through_verbatim(self) -> None:
        """No session expiry — empty prompt must NOT be swapped. Injecting
        a cue every continuation makes the model parrot it back ('user
        keeps saying continue from where you left off')."""
        backend = _backend()
        backend._session = MagicMock()
        backend._client = MagicMock()

        seen_prompts: list[str] = []

        async def _do_stream_fake(prompt: str, **_: Any) -> Any:
            seen_prompts.append(prompt)
            if False:
                yield  # pragma: no cover
            return

        backend._do_stream = _do_stream_fake  # type: ignore[assignment]

        async for _ in backend.stream("", messages=_make_messages()):
            pass

        assert seen_prompts == [""], (
            "Empty post-tool prompts must not be modified on the healthy path."
        )

    async def test_recovery_retry_swaps_empty_to_internal_cue(self) -> None:
        """When the first call raises session-expired, the retry must use
        the cue (the freshly-recovered session has no implicit context)."""
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

        seen_prompts: list[str] = []
        call_count = {"n": 0}

        async def _do_stream_fake(prompt: str, **_: Any) -> Any:
            call_count["n"] += 1
            seen_prompts.append(prompt)
            if call_count["n"] == 1:
                raise RuntimeError("session not found")
            if False:
                yield  # pragma: no cover
            return

        backend._do_stream = _do_stream_fake  # type: ignore[assignment]

        async for _ in backend.stream("", messages=_make_messages()):
            pass

        assert seen_prompts == ["", backend._CONTINUATION_CUE], (
            "Healthy-path call must be empty; recovery retry must use the cue."
        )

    async def test_recovery_retry_preserves_real_prompt(self) -> None:
        """A real user prompt at recovery time must be replayed verbatim,
        not replaced by the cue."""
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

        seen_prompts: list[str] = []
        call_count = {"n": 0}

        async def _do_stream_fake(prompt: str, **_: Any) -> Any:
            call_count["n"] += 1
            seen_prompts.append(prompt)
            if call_count["n"] == 1:
                raise RuntimeError("session not found")
            if False:
                yield  # pragma: no cover
            return

        backend._do_stream = _do_stream_fake  # type: ignore[assignment]

        async for _ in backend.stream("hello there", messages=_make_messages()):
            pass

        assert seen_prompts == ["hello there", "hello there"]

    def test_helper_swaps_only_empty(self) -> None:
        cue = CopilotBackend._CONTINUATION_CUE
        assert CopilotBackend._prompt_for_recovery_retry("") == cue
        assert CopilotBackend._prompt_for_recovery_retry("   \n\t") == cue
        assert CopilotBackend._prompt_for_recovery_retry("real") == "real"

    def test_internal_cue_is_clearly_framed(self) -> None:
        """Sanity: the cue must contain the harness-internal markers and
        the do-not-echo / do-not-thank guidance — that's what stops the
        model from leaking it back into user-facing output."""
        cue = CopilotBackend._CONTINUATION_CUE
        assert "[internal:obscura-harness]" in cue
        assert "[/internal:obscura-harness]" in cue
        assert "did NOT type" in cue
        assert "Do not echo" in cue


class TestEmptyPromptProbe:
    """``_maybe_log_empty_prompt`` is the diagnostic that pins down who's
    calling ``backend.send("")`` — that's the source of the phantom blank
    ``user.message`` events the model rationalises as "user sent blank"."""

    def test_logs_at_warning_when_env_var_set(
        self,
        monkeypatch: Any,
        caplog: Any,
    ) -> None:
        import importlib
        import logging

        monkeypatch.setenv("OBSCURA_DEBUG_EMPTY_PROMPT", "1")
        # Re-import the module so the env-var-driven flag is re-read.
        from obscura.providers import copilot as copilot_mod

        importlib.reload(copilot_mod)
        try:
            log = logging.getLogger("test-probe")
            with caplog.at_level(logging.WARNING, logger="test-probe"):
                copilot_mod._maybe_log_empty_prompt("send", "", {"foo": 1}, log)
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert any("EMPTY_PROMPT_PROBE" in r.message for r in warnings)
            # The kwargs keys are surfaced so we can tell title-gen apart
            # from continuation passes that include ``messages=``.
            assert any("'foo'" in r.message for r in warnings)
        finally:
            # Revert so other tests in the same process see the default.
            monkeypatch.delenv("OBSCURA_DEBUG_EMPTY_PROMPT", raising=False)
            importlib.reload(copilot_mod)

    def test_silent_for_non_empty_prompt(self, caplog: Any) -> None:
        import logging

        from obscura.providers.copilot import _maybe_log_empty_prompt

        log = logging.getLogger("test-probe-silent")
        with caplog.at_level(logging.DEBUG, logger="test-probe-silent"):
            _maybe_log_empty_prompt("send", "real text", {}, log)
        assert not any("EMPTY_PROMPT_PROBE" in r.message for r in caplog.records)

    def test_whitespace_only_treated_as_empty(self, caplog: Any) -> None:
        import logging

        from obscura.providers.copilot import _maybe_log_empty_prompt

        log = logging.getLogger("test-probe-ws")
        log.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG, logger="test-probe-ws"):
            _maybe_log_empty_prompt("send", "  \n\t  ", {}, log)
        assert any("EMPTY_PROMPT_PROBE" in r.message for r in caplog.records)


class TestSendIsolated:
    """``send_isolated`` must run on an ephemeral session so the live
    Copilot conversation history isn't polluted by title-gen / consolidator
    / arbiter calls."""

    async def test_creates_temp_session_and_does_not_mutate_self_session(
        self,
    ) -> None:
        backend = _backend()
        live_session = MagicMock()
        live_session.session_id = "live-sid"
        backend._session = live_session

        temp_session = MagicMock()
        temp_session.session_id = "temp-sid"
        temp_response = MagicMock()
        temp_response.content = []
        temp_session.send_and_wait = AsyncMock(return_value=temp_response)
        temp_session.close = MagicMock()

        client = MagicMock()
        client.create_session = AsyncMock(return_value=temp_session)
        backend._client = client

        await backend.send_isolated("title please")

        # We must have created a NEW session (not reused the live one).
        client.create_session.assert_awaited_once()
        # The live session must NOT have been touched.
        assert backend._session is live_session
        # The send happened on the temp session, not the live one.
        temp_session.send_and_wait.assert_awaited_once()
        live_session.send_and_wait.assert_not_called()
        # And the temp session was cleaned up after.
        temp_session.close.assert_called_once()

    async def test_close_failure_does_not_propagate(self) -> None:
        backend = _backend()
        backend._session = MagicMock()

        temp_session = MagicMock()
        temp_response = MagicMock()
        temp_response.content = []
        temp_session.send_and_wait = AsyncMock(return_value=temp_response)
        temp_session.close = MagicMock(side_effect=RuntimeError("boom"))

        client = MagicMock()
        client.create_session = AsyncMock(return_value=temp_session)
        backend._client = client

        # Cleanup failure must not raise — the response is what callers care
        # about.
        await backend.send_isolated("anything")

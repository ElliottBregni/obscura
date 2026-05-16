"""Tests for `_TypingTracker` — WhatsApp typing indicator with keepalive.

The tracker calls ``WuzapiClient.set_chat_presence`` with state=composing
on start (plus every refresh_interval_s) and state=paused on stop or
max-duration. All presence errors must be swallowed — the bubble is
best-effort and must never block a real reply.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.integrations.whatsapp.wuzapi.service import _TypingTracker


def _make_client() -> Any:
    """Mock WuzapiClient with an AsyncMock set_chat_presence."""
    client = MagicMock()
    client.set_chat_presence = AsyncMock(return_value=None)
    return client


def _states_sent(client: Any) -> list[str]:
    """Extract the ``state`` arg from every set_chat_presence call."""
    return [call.kwargs["state"] for call in client.set_chat_presence.call_args_list]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_sends_composing_immediately() -> None:
    """The first composing fires synchronously inside start() so the
    bubble appears as soon as the message hits the queue."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    client.set_chat_presence.assert_awaited_once_with(
        "alice@s.whatsapp.net",
        state="composing",
    )
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    """Calling start twice doesn't double-fire composing."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.start("alice@s.whatsapp.net")
    assert client.set_chat_presence.await_count == 1
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_sends_paused_and_cancels_keepalive() -> None:
    """stop() ends the keepalive and clears the indicator."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.stop("alice@s.whatsapp.net")
    states = _states_sent(client)
    assert states == ["composing", "paused"]
    assert "alice@s.whatsapp.net" not in tracker._tasks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_with_no_active_tracker_still_sends_paused() -> None:
    """stop() on a recipient that was never start()ed still sends
    paused — useful for clearing stale indicators from previous runs."""
    client = _make_client()
    tracker = _TypingTracker(client)
    await tracker.stop("alice@s.whatsapp.net")
    client.set_chat_presence.assert_awaited_once_with(
        "alice@s.whatsapp.net",
        state="paused",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keepalive_refreshes_composing() -> None:
    """Keepalive re-sends composing every refresh_interval_s."""
    client = _make_client()
    tracker = _TypingTracker(
        client,
        refresh_interval_s=0.02,
        max_duration_s=10.0,
    )
    await tracker.start("alice@s.whatsapp.net")
    # Let several refreshes fire
    await asyncio.sleep(0.1)
    composing_count = sum(1 for s in _states_sent(client) if s == "composing")
    assert composing_count >= 3
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keepalive_auto_clears_on_max_duration() -> None:
    """When max_duration_s elapses, the keepalive sends paused and exits
    without anyone calling stop()."""
    client = _make_client()
    tracker = _TypingTracker(
        client,
        refresh_interval_s=0.01,
        max_duration_s=0.05,
    )
    await tracker.start("alice@s.whatsapp.net")
    # Wait past the max duration
    await asyncio.sleep(0.2)
    states = _states_sent(client)
    assert states[0] == "composing"
    assert "paused" in states
    assert "alice@s.whatsapp.net" not in tracker._tasks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_presence_errors_are_swallowed() -> None:
    """A failing set_chat_presence (network blip, wuzapi down) does not
    raise from start/stop — typing is best-effort."""
    client = MagicMock()
    client.set_chat_presence = AsyncMock(
        side_effect=RuntimeError("wuzapi unreachable"),
    )
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    # Both of these would raise without exception suppression
    await tracker.start("alice@s.whatsapp.net")
    await tracker.stop("alice@s.whatsapp.net")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_recipients_tracked_independently() -> None:
    """Each recipient has its own keepalive task; stopping one doesn't
    affect another."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.start("bob@s.whatsapp.net")
    assert len(tracker._tasks) == 2
    await tracker.stop("alice@s.whatsapp.net")
    assert "alice@s.whatsapp.net" not in tracker._tasks
    assert "bob@s.whatsapp.net" in tracker._tasks
    tracker.cancel_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_all_clears_all_tasks() -> None:
    """cancel_all() ends every keepalive — used at service shutdown."""
    client = _make_client()
    tracker = _TypingTracker(client, refresh_interval_s=60.0)
    await tracker.start("alice@s.whatsapp.net")
    await tracker.start("bob@s.whatsapp.net")
    tracker.cancel_all()
    assert len(tracker._tasks) == 0


# ---------------------------------------------------------------------------
# _strip_device_suffix — JID normalization for self-chat announcements
# ---------------------------------------------------------------------------


def test_strip_device_suffix_with_device() -> None:
    """The linked-device JID 'phone:device@server' becomes 'phone@server'."""
    from obscura.integrations.whatsapp.wuzapi.service import _strip_device_suffix

    assert (
        _strip_device_suffix("12316333624:14@s.whatsapp.net")
        == "12316333624@s.whatsapp.net"
    )


def test_strip_device_suffix_without_device() -> None:
    """JID without a device segment passes through unchanged."""
    from obscura.integrations.whatsapp.wuzapi.service import _strip_device_suffix

    assert (
        _strip_device_suffix("12316333624@s.whatsapp.net")
        == "12316333624@s.whatsapp.net"
    )


def test_strip_device_suffix_no_at_sign() -> None:
    """Edge case: input without '@' returns unchanged (don't synthesize a server)."""
    from obscura.integrations.whatsapp.wuzapi.service import _strip_device_suffix

    assert _strip_device_suffix("12316333624:14") == "12316333624:14"


def test_strip_device_suffix_group_jid() -> None:
    """Group JIDs (@g.us) shouldn't be affected — groups don't have device suffixes."""
    from obscura.integrations.whatsapp.wuzapi.service import _strip_device_suffix

    assert (
        _strip_device_suffix("12316333624-1234567890@g.us")
        == "12316333624-1234567890@g.us"
    )


# ---------------------------------------------------------------------------
# _should_route_inbound — conversation-aware ACL policy table
# ---------------------------------------------------------------------------


@pytest.fixture
def _allowlist_config(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point command_acl at a temp config.toml with a known reply_allowlist."""
    from textwrap import dedent

    monkeypatch.setattr(
        "obscura.integrations.messaging.command_acl.resolve_obscura_home",
        lambda: tmp_path,
    )
    (tmp_path / "config.toml").write_text(
        dedent(
            """
        [messaging.whatsapp]
        reply_allowlist = ["2316333624"]
        """,
        )
    )
    return tmp_path


def test_route_self_chat_lid_form_allowed(_allowlist_config: Any) -> None:
    """Self-chat with both Chat and Sender in LID form."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="187437204672730",
        chat_jid="187437204672730@lid",
        is_from_me=True,
    )
    assert allow is True
    assert reason.startswith("self-chat")


def test_route_self_chat_phone_form_allowed(_allowlist_config: Any) -> None:
    """Self-chat with both Chat and Sender in phone-JID form."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="12316333624",
        chat_jid="12316333624@s.whatsapp.net",
        is_from_me=True,
    )
    assert allow is True
    assert reason.startswith("self-chat")


def test_route_self_chat_mixed_phone_chat_lid_sender(_allowlist_config: Any) -> None:
    """The bug Elliott actually hit: Chat=phone, Sender=LID for the
    same self-chat. chat==sender comparison fails (different forms),
    but the self_jid_digits reference makes the match work because
    chat matches the linked device's phone JID."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="187437204672730",
        chat_jid="12316333624@s.whatsapp.net",
        is_from_me=True,
        self_jid_digits="2316333624",  # phone digits after country-code strip
    )
    assert allow is True
    assert "linked device" in reason


def test_route_self_chat_mixed_chat_lid_phone_sender(_allowlist_config: Any) -> None:
    """Inverse of the above: Chat=LID, Sender=phone. Sender matches
    the linked device's phone JID."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="12316333624",
        chat_jid="187437204672730@lid",
        is_from_me=True,
        self_jid_digits="2316333624",
    )
    assert allow is True
    assert "linked device" in reason


def test_route_mixed_form_without_self_jid_falls_through(
    _allowlist_config: Any,
) -> None:
    """If session_status fetch failed (self_jid_digits empty), the
    Chat=phone/Sender=LID mixed case has no way to resolve and falls
    through to the DM-intercept-deny branch. This is the pre-fix
    behavior — documented so it's clear the self_jid path is what
    saves it."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="187437204672730",
        chat_jid="12316333624@s.whatsapp.net",
        is_from_me=True,
        self_jid_digits="",  # session_status failed
    )
    assert allow is False
    assert "user-typed" in reason


def test_route_inbound_from_allowlisted_sender_allowed(_allowlist_config: Any) -> None:
    """A non-self message from an allowlisted sender is allowed."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, _reason = _should_route_inbound(
        sender_id="2316333624",
        chat_jid="2316333624@s.whatsapp.net",
        is_from_me=False,
    )
    assert allow is True


def test_route_inbound_from_friend_denied(_allowlist_config: Any) -> None:
    """The original 'AI texted my friend' bug: friend's message comes
    in, friend not in allowlist, dropped before reaching agent."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="5551234567",
        chat_jid="5551234567@s.whatsapp.net",
        is_from_me=False,
    )
    assert allow is False
    assert "non-allowlisted sender" in reason


def test_route_user_typing_to_friend_denied(_allowlist_config: Any) -> None:
    """User typing in DM with a friend (IsFromMe=true, chat != sender)
    must not trigger the agent — would result in agent intercepting
    user's outbound to others."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="187437204672730",  # user's LID
        chat_jid="5551234567@s.whatsapp.net",  # friend's JID
        is_from_me=True,
    )
    assert allow is False
    assert "user-typed" in reason


def test_route_group_chat_denied_by_default(_allowlist_config: Any) -> None:
    """Group chats are denied by default, even if the user is the sender."""
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, reason = _should_route_inbound(
        sender_id="187437204672730",
        chat_jid="12345-67890@g.us",
        is_from_me=True,
    )
    assert allow is False
    assert "group" in reason


def test_route_group_chat_allowed_if_jid_in_allowlist(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a specific group JID is in reply_allowlist, messages in that
    group are routed through to the agent."""
    from textwrap import dedent

    monkeypatch.setattr(
        "obscura.integrations.messaging.command_acl.resolve_obscura_home",
        lambda: tmp_path,
    )
    (tmp_path / "config.toml").write_text(
        dedent(
            """
        [messaging.whatsapp]
        reply_allowlist = ["12345-67890@g.us"]
        """,
        )
    )
    from obscura.integrations.whatsapp.wuzapi.service import _should_route_inbound

    allow, _reason = _should_route_inbound(
        sender_id="5551234567",
        chat_jid="12345-67890@g.us",
        is_from_me=False,
    )
    # NOTE: digit-normalization in command_acl strips non-digits, so the
    # group JID matches by its digit content. This is best-effort group
    # allowlisting — if you need exact group JID matching, the helper
    # would need a separate path.
    # The current behavior: digits "1234567890" extracted from
    # "12345-67890@g.us" match if "1234567890" appears in the list.
    # Since "12345-67890@g.us" normalizes to "1234567890", and the
    # allowlist entry "12345-67890@g.us" normalizes to "1234567890",
    # they match.
    assert allow is True

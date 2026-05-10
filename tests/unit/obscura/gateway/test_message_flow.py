"""Unit tests for the gateway message flow.

Tests the full chain:
    PlatformMessage
      → GatewayNetworkBridge.dispatch() / dispatch_await()
      → ChannelRouter (session + history)
      → GatewayAgentRunner.run_turn()
      → GatewayOrchestrator.execute_tool("spawn_agent")
      → response string
      → adapter.send(recipient, text)

GatewayOrchestrator.execute_tool is mocked so these tests exercise
everything above the orchestrator (bridge, router, session store,
dedup, agent runner) without touching any LLM backend.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.gateway.config import GatewayConfig, GatewayMode
from obscura.gateway.network_bridge import GatewayAgentRunner, GatewayNetworkBridge
from obscura.gateway.orchestrator import GatewayOrchestrator, GatewayState
from obscura.gateway.poll_daemon import GatewayPollDaemon
from obscura.integrations.messaging.identity import build_conversation_key
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.messaging.router import ChannelRouter, ChannelRouterConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(
    platform: str = "imessage",
    sender: str = "+15550001234",
    text: str = "hello",
    message_id: str | None = None,
) -> PlatformMessage:
    return PlatformMessage(
        platform=platform,
        account_id="test",
        channel_id=f"dm:{sender}",
        sender_id=sender,
        recipient_id="me",
        message_id=message_id or f"{platform}-msg-001",
        text=text,
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={},
    )


def _conv_key(
    platform: str = "whatsapp",
    sender: str = "+15550001234",
) -> str:
    return build_conversation_key(
        platform=platform,
        account_id="test",
        channel_id=f"dm:{sender}",
        participants=[sender],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orchestrator() -> MagicMock:
    orc = MagicMock(spec=GatewayOrchestrator)
    orc.state = GatewayState.RUNNING
    orc._current_mode = GatewayMode.NATIVE
    orc.execute_tool = AsyncMock(
        return_value={"response": "hello back", "session_id": "test-session"}
    )
    orc.start = AsyncMock()
    orc.stop = AsyncMock()
    orc.switch_mode = AsyncMock(return_value=True)
    orc.get_status = AsyncMock(
        return_value={
            "state": "RUNNING",
            "mode": "NATIVE",
            "config": {},
            "mode_status": {},
        }
    )
    return orc


@pytest.fixture
def mock_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=True)
    return adapter


@pytest.fixture
def bridge(mock_orchestrator: MagicMock) -> GatewayNetworkBridge:
    runner = GatewayAgentRunner(mock_orchestrator)
    router = ChannelRouter(
        runner=runner,
        config=ChannelRouterConfig(send_typing_indicator=False),
    )
    b = GatewayNetworkBridge(mock_orchestrator, router)
    b._started = True  # skip actual start()
    return b


# ---------------------------------------------------------------------------
# 1. GatewayAgentRunner calls orchestrator
# ---------------------------------------------------------------------------


async def test_gateway_agent_runner_calls_orchestrator(
    mock_orchestrator: MagicMock,
) -> None:
    runner = GatewayAgentRunner(mock_orchestrator)
    result = await runner.run_turn(
        "hello",
        session_id="s1",
        history=[],
        system_prompt="be helpful",
        max_turns=4,
    )

    mock_orchestrator.execute_tool.assert_called_once_with(
        "spawn_agent",
        prompt="hello",
        context=[],
        session_id="s1",
        system_prompt="be helpful",
        max_turns=4,
    )
    assert result == "hello back"


# ---------------------------------------------------------------------------
# 2. GatewayAgentRunner auto-starts orchestrator when INITIALIZING
# ---------------------------------------------------------------------------


async def test_gateway_agent_runner_auto_starts_orchestrator(
    mock_orchestrator: MagicMock,
) -> None:
    mock_orchestrator.state = GatewayState.INITIALIZING

    runner = GatewayAgentRunner(mock_orchestrator)
    await runner.run_turn(
        "hi",
        session_id="s2",
        history=[],
        system_prompt="",
        max_turns=1,
    )

    mock_orchestrator.start.assert_called_once()


# ---------------------------------------------------------------------------
# 3. GatewayAgentRunner never raises — errors return a string
# ---------------------------------------------------------------------------


async def test_gateway_agent_runner_never_raises(
    mock_orchestrator: MagicMock,
) -> None:
    mock_orchestrator.execute_tool = AsyncMock(side_effect=RuntimeError("boom"))

    runner = GatewayAgentRunner(mock_orchestrator)
    result = await runner.run_turn(
        "hello",
        session_id="s3",
        history=[],
        system_prompt="",
        max_turns=1,
    )

    assert isinstance(result, str)
    assert "unavailable" in result.lower() or "error" in result.lower()


# ---------------------------------------------------------------------------
# 4. dispatch() routes to adapter.send() — iMessage
# ---------------------------------------------------------------------------


async def test_dispatch_routes_to_adapter_send(
    bridge: GatewayNetworkBridge,
    mock_adapter: MagicMock,
) -> None:
    bridge.router.register("imessage", mock_adapter)
    await bridge.dispatch(_make_msg("imessage"))

    mock_adapter.send.assert_called_once()
    call_args = mock_adapter.send.call_args
    assert call_args[0][0] == "+15550001234"  # recipient
    assert call_args[0][1] == "hello back"    # response text


# ---------------------------------------------------------------------------
# 5. dispatch() routes — WhatsApp
# ---------------------------------------------------------------------------


async def test_dispatch_whatsapp(
    bridge: GatewayNetworkBridge,
    mock_adapter: MagicMock,
) -> None:
    bridge.router.register("whatsapp", mock_adapter)
    await bridge.dispatch(_make_msg("whatsapp"))

    mock_adapter.send.assert_called_once()
    call_args = mock_adapter.send.call_args
    assert call_args[0][1] == "hello back"


# ---------------------------------------------------------------------------
# 6. dispatch() routes — Discord
# ---------------------------------------------------------------------------


async def test_dispatch_discord(
    bridge: GatewayNetworkBridge,
    mock_adapter: MagicMock,
) -> None:
    bridge.router.register("discord", mock_adapter)
    await bridge.dispatch(_make_msg("discord"))

    mock_adapter.send.assert_called_once()
    call_args = mock_adapter.send.call_args
    assert call_args[0][1] == "hello back"


# ---------------------------------------------------------------------------
# 7. Duplicate message_id is deduped
# ---------------------------------------------------------------------------


async def test_dispatch_deduplicates(
    bridge: GatewayNetworkBridge,
    mock_adapter: MagicMock,
    mock_orchestrator: MagicMock,
) -> None:
    bridge.router.register("imessage", mock_adapter)

    msg = _make_msg("imessage", message_id="dedup-001")
    await bridge.dispatch(msg)
    await bridge.dispatch(msg)  # same message_id — should be deduped

    mock_orchestrator.execute_tool.assert_called_once()
    mock_adapter.send.assert_called_once()


# ---------------------------------------------------------------------------
# 8. dispatch_await() returns response string and skips adapter.send()
# ---------------------------------------------------------------------------


async def test_dispatch_await_returns_response_string(
    bridge: GatewayNetworkBridge,
    mock_adapter: MagicMock,
) -> None:
    bridge.router.register("whatsapp", mock_adapter)

    response = await bridge.dispatch_await(_make_msg("whatsapp"))

    assert response == "hello back"
    assert mock_adapter.send.call_count == 0


# ---------------------------------------------------------------------------
# 9. dispatch_await() persists session history across turns
# ---------------------------------------------------------------------------


async def test_dispatch_await_persists_session_history(
    mock_orchestrator: MagicMock,
    mock_adapter: MagicMock,
) -> None:
    # Use a fresh bridge+router so ConversationStore starts empty
    runner = GatewayAgentRunner(mock_orchestrator)
    router = ChannelRouter(
        runner=runner,
        config=ChannelRouterConfig(send_typing_indicator=False),
    )
    fresh_bridge = GatewayNetworkBridge(mock_orchestrator, router)
    fresh_bridge._started = True
    fresh_bridge.router.register("whatsapp", mock_adapter)

    sender = "+15559990001"
    msg1 = PlatformMessage(
        platform="whatsapp",
        account_id="test",
        channel_id=f"dm:{sender}",
        sender_id=sender,
        recipient_id="me",
        message_id="history-msg-001",
        text="first message",
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={},
    )
    msg2 = PlatformMessage(
        platform="whatsapp",
        account_id="test",
        channel_id=f"dm:{sender}",
        sender_id=sender,
        recipient_id="me",
        message_id="history-msg-002",
        text="second message",
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={},
    )

    await fresh_bridge.dispatch_await(msg1)
    await fresh_bridge.dispatch_await(msg2)

    conv_key = build_conversation_key(
        platform="whatsapp",
        account_id="test",
        channel_id=f"dm:{sender}",
        participants=[sender],
    )
    history = await fresh_bridge.get_session_context(conv_key)

    assert history is not None
    # 2 user turns + 2 assistant turns = 4 entries
    assert len(history) == 4
    roles = [entry["role"] for entry in history]
    assert roles == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# 10. clear_session() wipes history and returns True
# ---------------------------------------------------------------------------


async def test_clear_session(
    bridge: GatewayNetworkBridge,
    mock_adapter: MagicMock,
) -> None:
    bridge.router.register("whatsapp", mock_adapter)

    # Create a session by dispatching one message
    await bridge.dispatch_await(_make_msg("whatsapp", message_id="msg-setup"))

    conv_key = _conv_key("whatsapp")
    # Verify session exists with history
    history_before = await bridge.get_session_context(conv_key)
    assert history_before is not None
    assert len(history_before) > 0

    cleared = await bridge.clear_session(conv_key)
    assert cleared is True

    history_after = await bridge.get_session_context(conv_key)
    assert history_after == []


# ---------------------------------------------------------------------------
# 11. GatewayPollDaemon dispatches messages from all platforms
# ---------------------------------------------------------------------------


async def test_poll_daemon_dispatches_all_platforms(
    bridge: GatewayNetworkBridge,
    mock_orchestrator: MagicMock,
) -> None:
    daemon = GatewayPollDaemon(bridge, poll_interval=0.0)

    # Build per-platform adapters with distinct message_ids to avoid dedup
    imessage_adapter = MagicMock()
    imessage_adapter.poll = AsyncMock(
        return_value=[_make_msg("imessage", message_id="im-poll-001")]
    )
    imessage_adapter.send = AsyncMock(return_value=True)

    whatsapp_adapter = MagicMock()
    whatsapp_adapter.poll = AsyncMock(
        return_value=[_make_msg("whatsapp", message_id="wa-poll-001")]
    )
    whatsapp_adapter.send = AsyncMock(return_value=True)

    discord_adapter = MagicMock()
    discord_adapter.poll = AsyncMock(
        return_value=[_make_msg("discord", message_id="dc-poll-001")]
    )
    discord_adapter.send = AsyncMock(return_value=True)

    # Register with both daemon (for poll) and router (for send)
    daemon.register("imessage", imessage_adapter)
    daemon.register("whatsapp", whatsapp_adapter)
    daemon.register("discord", discord_adapter)
    bridge.router.register("imessage", imessage_adapter)
    bridge.router.register("whatsapp", whatsapp_adapter)
    bridge.router.register("discord", discord_adapter)

    await daemon.start()
    await asyncio.sleep(0.1)
    await daemon.stop()

    imessage_adapter.poll.assert_called()
    whatsapp_adapter.poll.assert_called()
    discord_adapter.poll.assert_called()

    # At least one execute_tool call per platform (3 messages dispatched)
    assert mock_orchestrator.execute_tool.call_count >= 3


# ---------------------------------------------------------------------------
# 12. GatewayPollDaemon isolates errors — healthy platform still sends
# ---------------------------------------------------------------------------


async def test_poll_daemon_isolates_platform_errors(
    bridge: GatewayNetworkBridge,
) -> None:
    daemon = GatewayPollDaemon(bridge, poll_interval=0.0)

    # Broken adapter — poll raises
    broken_adapter = MagicMock()
    broken_adapter.poll = AsyncMock(side_effect=RuntimeError("adapter down"))
    broken_adapter.send = AsyncMock(return_value=True)

    # Healthy adapter — returns a message
    healthy_adapter = MagicMock()
    healthy_adapter.poll = AsyncMock(
        return_value=[_make_msg("whatsapp", message_id="healthy-001")]
    )
    healthy_adapter.send = AsyncMock(return_value=True)

    daemon.register("imessage", broken_adapter)
    daemon.register("whatsapp", healthy_adapter)
    bridge.router.register("imessage", broken_adapter)
    bridge.router.register("whatsapp", healthy_adapter)

    await daemon.start()
    await asyncio.sleep(0.1)
    await daemon.stop()

    # The healthy adapter's send should have been called
    healthy_adapter.send.assert_called()
    # The broken adapter's poll was called but raised; daemon kept running
    broken_adapter.poll.assert_called()

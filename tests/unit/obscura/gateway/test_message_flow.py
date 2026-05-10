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
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.gateway.config import GatewayConfig, GatewayMode
from obscura.gateway.network_bridge import GatewayAgentRunner, GatewayNetworkBridge
from obscura.gateway.orchestrator import GatewayOrchestrator, GatewayState
from obscura.gateway.poll_daemon import GatewayPollDaemon
from obscura.integrations.messaging.identity import build_conversation_key
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.messaging.router import ChannelRouter, ChannelRouterConfig
from obscura.integrations.messaging.store import ConversationStore, MessageDedupeStore

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
    """Build a PlatformMessage with a unique message_id by default."""
    return PlatformMessage(
        platform=platform,
        account_id="test",
        channel_id=f"dm:{sender}",
        sender_id=sender,
        recipient_id="me",
        message_id=message_id or str(uuid.uuid4()),
        text=text,
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={},
    )


def _build_bridge(
    mock_orchestrator: MagicMock,
    db_path: Path,
) -> GatewayNetworkBridge:
    """Build an isolated bridge with its own SQLite state in *db_path*."""
    store = ConversationStore(db_path=db_path)
    dedupe = MessageDedupeStore(db_path=db_path)
    runner = GatewayAgentRunner(mock_orchestrator)
    router = ChannelRouter(
        runner=runner,
        config=ChannelRouterConfig(send_typing_indicator=False),
        store=store,
        dedupe=dedupe,
    )
    b = GatewayNetworkBridge(mock_orchestrator, router)
    b._started = True  # skip actual start()
    return b


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
def bridge(mock_orchestrator: MagicMock, tmp_path: Path) -> GatewayNetworkBridge:
    return _build_bridge(mock_orchestrator, tmp_path / "state.db")


# ---------------------------------------------------------------------------
# 1. GatewayAgentRunner calls orchestrator with correct args
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
# 7. Same message_id dispatched twice is deduped
# ---------------------------------------------------------------------------


async def test_dispatch_deduplicates(
    bridge: GatewayNetworkBridge,
    mock_adapter: MagicMock,
    mock_orchestrator: MagicMock,
) -> None:
    bridge.router.register("imessage", mock_adapter)

    fixed_id = "dedup-" + str(uuid.uuid4())
    msg = _make_msg("imessage", message_id=fixed_id)
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
    tmp_path: Path,
    mock_adapter: MagicMock,
) -> None:
    # Fresh isolated bridge so the store starts empty
    fresh_bridge = _build_bridge(mock_orchestrator, tmp_path / "hist_state.db")
    fresh_bridge.router.register("whatsapp", mock_adapter)

    sender = "+15559990001"

    def _wa_msg(text: str) -> PlatformMessage:
        return PlatformMessage(
            platform="whatsapp",
            account_id="test",
            channel_id=f"dm:{sender}",
            sender_id=sender,
            recipient_id="me",
            message_id=str(uuid.uuid4()),
            text=text,
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={},
        )

    await fresh_bridge.dispatch_await(_wa_msg("first message"))
    await fresh_bridge.dispatch_await(_wa_msg("second message"))

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
    mock_orchestrator: MagicMock,
    tmp_path: Path,
    mock_adapter: MagicMock,
) -> None:
    fresh_bridge = _build_bridge(mock_orchestrator, tmp_path / "clear_state.db")
    fresh_bridge.router.register("whatsapp", mock_adapter)

    sender = "+15558887777"
    msg = PlatformMessage(
        platform="whatsapp",
        account_id="test",
        channel_id=f"dm:{sender}",
        sender_id=sender,
        recipient_id="me",
        message_id=str(uuid.uuid4()),
        text="hello",
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={},
    )
    await fresh_bridge.dispatch_await(msg)

    conv_key = build_conversation_key(
        platform="whatsapp",
        account_id="test",
        channel_id=f"dm:{sender}",
        participants=[sender],
    )

    history_before = await fresh_bridge.get_session_context(conv_key)
    assert history_before is not None
    assert len(history_before) > 0

    cleared = await fresh_bridge.clear_session(conv_key)
    assert cleared is True

    history_after = await fresh_bridge.get_session_context(conv_key)
    assert history_after == []


# ---------------------------------------------------------------------------
# 11. GatewayPollDaemon dispatches messages from all platforms
# ---------------------------------------------------------------------------


async def test_poll_daemon_dispatches_all_platforms(
    mock_orchestrator: MagicMock,
    tmp_path: Path,
) -> None:
    fresh_bridge = _build_bridge(mock_orchestrator, tmp_path / "poll_state.db")
    daemon = GatewayPollDaemon(fresh_bridge, poll_interval=0.05)

    # Build per-platform adapters — unique message_ids so dedup doesn't suppress
    imessage_adapter = MagicMock()
    imessage_adapter.poll = AsyncMock(
        return_value=[_make_msg("imessage")]
    )
    imessage_adapter.send = AsyncMock(return_value=True)

    whatsapp_adapter = MagicMock()
    whatsapp_adapter.poll = AsyncMock(
        return_value=[_make_msg("whatsapp")]
    )
    whatsapp_adapter.send = AsyncMock(return_value=True)

    discord_adapter = MagicMock()
    discord_adapter.poll = AsyncMock(
        return_value=[_make_msg("discord")]
    )
    discord_adapter.send = AsyncMock(return_value=True)

    # Register with both daemon (poll loop) and router (send replies)
    daemon.register("imessage", imessage_adapter)
    daemon.register("whatsapp", whatsapp_adapter)
    daemon.register("discord", discord_adapter)
    fresh_bridge.router.register("imessage", imessage_adapter)
    fresh_bridge.router.register("whatsapp", whatsapp_adapter)
    fresh_bridge.router.register("discord", discord_adapter)

    await daemon.start()
    await asyncio.sleep(0.2)
    await daemon.stop()

    imessage_adapter.poll.assert_called()
    whatsapp_adapter.poll.assert_called()
    discord_adapter.poll.assert_called()

    # At least one execute_tool call per platform
    assert mock_orchestrator.execute_tool.call_count >= 3


# ---------------------------------------------------------------------------
# 12. GatewayPollDaemon isolates platform errors
# ---------------------------------------------------------------------------


async def test_poll_daemon_isolates_platform_errors(
    mock_orchestrator: MagicMock,
    tmp_path: Path,
) -> None:
    fresh_bridge = _build_bridge(mock_orchestrator, tmp_path / "isolate_state.db")
    daemon = GatewayPollDaemon(fresh_bridge, poll_interval=0.05)

    # Broken adapter — poll always raises
    broken_adapter = MagicMock()
    broken_adapter.poll = AsyncMock(side_effect=RuntimeError("adapter down"))
    broken_adapter.send = AsyncMock(return_value=True)

    # Healthy adapter — returns one message per poll
    healthy_adapter = MagicMock()
    healthy_adapter.poll = AsyncMock(
        return_value=[_make_msg("whatsapp")]
    )
    healthy_adapter.send = AsyncMock(return_value=True)

    daemon.register("imessage", broken_adapter)
    daemon.register("whatsapp", healthy_adapter)
    fresh_bridge.router.register("imessage", broken_adapter)
    fresh_bridge.router.register("whatsapp", healthy_adapter)

    await daemon.start()
    await asyncio.sleep(0.2)
    await daemon.stop()

    # Healthy platform's send was called despite the broken platform erroring
    healthy_adapter.send.assert_called()
    # Broken adapter's poll was attempted
    broken_adapter.poll.assert_called()

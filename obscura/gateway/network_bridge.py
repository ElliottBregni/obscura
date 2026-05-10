"""Gateway network bridge — wires GatewayOrchestrator into ChannelRouter.

This module is the seam between the gateway's mode-selection layer and the
messaging layer's ChannelRouter.  A ``GatewayAgentRunner`` implements
``AgentRunnerProtocol`` by delegating every turn to
``GatewayOrchestrator.execute_tool("spawn_agent", ...)``, so the active
gateway mode (OPENCLAW / NATIVE / MCP) is completely transparent to the
router.

Typical usage::

    bridge = await build_gateway_network_bridge()
    await bridge.start()

    # Route a message from any platform
    await bridge.dispatch(
        platform="imessage",
        sender_id="+14155550123",
        text="hello",
    )

    # Hot-swap mode at runtime
    await bridge.switch_gateway_mode(GatewayMode.NATIVE)

    await bridge.stop()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.gateway.config import GatewayConfig, GatewayMode
from obscura.gateway.orchestrator import GatewayOrchestrator, GatewayState
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.messaging.router import ChannelRouter, ChannelRouterConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GatewayAgentRunner
# ---------------------------------------------------------------------------


class GatewayAgentRunner:
    """AgentRunnerProtocol implementation that routes turns through GatewayOrchestrator.

    This bridges ChannelRouter (messaging) to GatewayOrchestrator (mode
    selection). The orchestrator transparently picks the best available mode
    (OPENCLAW > NATIVE > MCP) for each turn.

    The runner never raises — all exceptions are caught and converted to a
    user-facing error string so the ChannelRouter can still send a reply.
    """

    def __init__(self, orchestrator: GatewayOrchestrator) -> None:
        self.orchestrator = orchestrator

    async def _ensure_running(self) -> None:
        """Start the orchestrator if it is not already running."""
        if self.orchestrator.state in (GatewayState.INITIALIZING, GatewayState.SHUTDOWN):
            logger.info(
                "GatewayAgentRunner: orchestrator not running (state=%s), starting",
                self.orchestrator.state.name,
            )
            await self.orchestrator.start()

    async def run_turn(
        self,
        prompt: str,
        *,
        session_id: str,
        history: list[dict[str, str]],
        system_prompt: str,
        max_turns: int,
    ) -> str:
        """Run one agent turn through the active gateway mode.

        Args:
            prompt: The user's latest message text.
            session_id: Stable conversation key from ChannelRouter.
            history: Prior turns as ``[{"role": ..., "text": ...}]`` dicts.
            system_prompt: System prompt string from ChannelRouterConfig.
            max_turns: Maximum agentic turns to run.

        Returns:
            Full response text from the agent.  Never raises.
        """
        try:
            await self._ensure_running()

            if self.orchestrator.state == GatewayState.DEGRADED:
                logger.warning(
                    "GatewayAgentRunner: orchestrator is DEGRADED; attempting turn anyway"
                )

            result: Any = await self.orchestrator.execute_tool(
                "spawn_agent",
                prompt=prompt,
                context=history,
                session_id=session_id,
                system_prompt=system_prompt,
                max_turns=max_turns,
            )

            response: str = result.get("response", "") if isinstance(result, dict) else str(result)
            return response.strip() or "(no response)"

        except RuntimeError as exc:
            # Gateway not started / not running — surface a clear message
            logger.error("GatewayAgentRunner: RuntimeError during run_turn: %s", exc)
            return f"Gateway unavailable: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("GatewayAgentRunner: unexpected error during run_turn")
            return f"An error occurred while processing your message: {exc}"


# ---------------------------------------------------------------------------
# GatewayNetworkBridge
# ---------------------------------------------------------------------------


class GatewayNetworkBridge:
    """Connects GatewayOrchestrator to ChannelRouter.

    This is the top-level wiring class.  After ``start()``, any platform
    message dispatched to the embedded ChannelRouter is processed through
    the GatewayOrchestrator's active mode.

    Example::

        bridge = GatewayNetworkBridge(orchestrator, router)
        await bridge.start()

        await bridge.dispatch(
            platform="whatsapp",
            sender_id="+14155550123",
            text="hello",
        )

        # Hot-swap the gateway mode while messaging keeps running
        await bridge.switch_gateway_mode(GatewayMode.NATIVE)

        await bridge.stop()
    """

    def __init__(
        self,
        orchestrator: GatewayOrchestrator,
        router: ChannelRouter,
    ) -> None:
        self.orchestrator = orchestrator
        self.router = router
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the orchestrator (if not already running).

        The ChannelRouter itself requires no async start; its adapters are
        started separately via ``register_platform_adapters``.
        """
        if self.orchestrator.state not in (GatewayState.RUNNING, GatewayState.DEGRADED):
            logger.info("GatewayNetworkBridge: starting orchestrator")
            await self.orchestrator.start()
        else:
            logger.debug(
                "GatewayNetworkBridge: orchestrator already in state=%s, skipping start",
                self.orchestrator.state.name,
            )
        self._started = True
        logger.info(
            "GatewayNetworkBridge ready — gateway mode=%s",
            self.orchestrator._current_mode.name if self.orchestrator._current_mode else "unknown",
        )

    async def stop(self) -> None:
        """Stop the orchestrator and mark the bridge as not started."""
        if self._started:
            logger.info("GatewayNetworkBridge: stopping orchestrator")
            await self.orchestrator.stop()
            self._started = False

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, msg: PlatformMessage) -> None:
        """Pass a PlatformMessage through to ChannelRouter.dispatch_message().

        This is a convenience wrapper — callers can also call
        ``bridge.router.dispatch(...)`` directly.
        """
        await self.router.dispatch_message(msg)

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    async def switch_gateway_mode(self, mode: GatewayMode) -> bool:
        """Hot-swap the gateway mode while messaging keeps running.

        The ChannelRouter is unaffected — the runner it holds
        (``GatewayAgentRunner``) delegates to the orchestrator, which
        switches modes transparently.

        Args:
            mode: Target GatewayMode.

        Returns:
            True if the switch succeeded.
        """
        logger.info("GatewayNetworkBridge: switching gateway mode to %s", mode.name)
        success = await self.orchestrator.switch_mode(mode)
        if success:
            logger.info("GatewayNetworkBridge: mode switch to %s succeeded", mode.name)
        else:
            logger.warning(
                "GatewayNetworkBridge: mode switch to %s failed; staying in %s",
                mode.name,
                self.orchestrator._current_mode.name if self.orchestrator._current_mode else "unknown",
            )
        return success

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> dict[str, Any]:
        """Return combined status: gateway state + registered router adapters.

        Returns:
            A dict with ``gateway`` and ``router`` sub-dicts.
        """
        gateway_status = await self.orchestrator.get_status()
        router_status: dict[str, Any] = {
            "registered_platforms": list(self.router._adapters.keys()),
            "platform_modes": {
                platform: mode.value
                for platform, mode in self.router._platform_modes.items()
            },
            "max_concurrent": self.router._config.max_concurrent,
        }
        return {
            "gateway": gateway_status,
            "router": router_status,
            "bridge_started": self._started,
        }

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> GatewayNetworkBridge:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def build_gateway_network_bridge(
    gateway_config: GatewayConfig | None = None,
    router_config: ChannelRouterConfig | None = None,
) -> GatewayNetworkBridge:
    """Build a fully wired GatewayNetworkBridge.

    The runner injected into the ChannelRouter is a ``GatewayAgentRunner``,
    so all message turns flow::

        Platform
          → ChannelRouter
          → GatewayAgentRunner
          → GatewayOrchestrator
          → active mode (OPENCLAW / NATIVE / MCP)
          → response
          → adapter.send()

    The bridge is **not** started automatically — call ``await bridge.start()``
    (or use it as an async context manager) before dispatching messages.

    Args:
        gateway_config: Optional GatewayConfig.  Defaults to
            ``GatewayConfig.from_env()``.
        router_config: Optional ChannelRouterConfig.  Defaults to the
            ChannelRouter's built-in defaults.

    Returns:
        A fully wired but not-yet-started GatewayNetworkBridge.
    """
    config = gateway_config or GatewayConfig.from_env()
    orchestrator = GatewayOrchestrator(config)
    runner = GatewayAgentRunner(orchestrator)
    router = ChannelRouter(runner=runner, config=router_config)
    return GatewayNetworkBridge(orchestrator, router)

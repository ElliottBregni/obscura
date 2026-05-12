"""obscura.integrations.network_gateway.heartbeat — Periodic agent heartbeat.

Broadcasts a scheduled agent turn to all connected WS clients (and optionally
to platform channels) at a configurable interval.  Analogous to OpenClaw's
``heartbeat`` subsystem.

Configuration (all optional, via GatewayConfig):
    heartbeat_enabled   bool    False by default.
    heartbeat_interval  float   1800.0 s (30 min) — seconds between turns.
    heartbeat_prompt    str     Default system prompt for the heartbeat turn.
    heartbeat_target    str     "ws" | "last" — where to deliver the result.
                                "ws" = broadcast to all WS clients.
                                "last" = send to last active platform channel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.integrations.network_gateway.connections import ConnectionRegistry

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "You are running a scheduled heartbeat check. "
    "Briefly summarize any pending goals or items that need attention. "
    "Be concise."
)


class HeartbeatTask:
    """Manages the periodic heartbeat background task."""

    def __init__(
        self,
        registry: "ConnectionRegistry",
        *,
        interval: float = 1800.0,
        prompt: str = _DEFAULT_PROMPT,
        backend: str = "claude",
        target: str = "ws",
    ) -> None:
        self._registry = registry
        self._interval = interval
        self._prompt = prompt or _DEFAULT_PROMPT
        self._backend = backend
        self._target = target
        self._task: asyncio.Task[None] | None = None
        self._last_run: float = 0.0

    def start(self) -> asyncio.Task[None]:
        """Start the heartbeat background task."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._run())
        logger.info(
            "HeartbeatTask started: interval=%.0fs backend=%s target=%s",
            self._interval,
            self._backend,
            self._target,
        )
        return self._task

    def stop(self) -> None:
        """Cancel the heartbeat task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def last_run(self) -> float:
        """Unix timestamp of the last heartbeat run (0 if never run)."""
        return self._last_run

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self._fire()
        except asyncio.CancelledError:
            logger.info("HeartbeatTask stopped")
            raise

    async def _fire(self) -> None:
        """Run one heartbeat turn and deliver the result."""
        self._last_run = time.time()
        logger.info("HeartbeatTask: firing heartbeat turn")

        # Notify clients that heartbeat is starting
        await self._registry.broadcast(
            {
                "type": "heartbeat",
                "event": "start",
                "timestamp": self._last_run,
            }
        )

        try:
            from obscura.integrations.network_gateway.chat_completions import (
                _stream_agent,
            )

            accumulated: list[str] = []
            async for delta in _stream_agent(
                self._backend, self._backend, "", self._prompt
            ):
                accumulated.append(delta)
                await self._registry.broadcast(
                    {
                        "type": "heartbeat",
                        "event": "token",
                        "content": delta,
                    }
                )
            result = "".join(accumulated)
        except Exception:
            logger.exception("HeartbeatTask: agent turn failed")
            await self._registry.broadcast(
                {
                    "type": "heartbeat",
                    "event": "error",
                    "message": "heartbeat turn failed",
                }
            )
            return

        await self._registry.broadcast(
            {
                "type": "heartbeat",
                "event": "done",
                "result": result,
                "timestamp": time.time(),
            }
        )
        logger.info("HeartbeatTask: done, result_len=%d", len(result))

        # Deliver to last active platform channel if target == "last"
        if self._target == "last":
            reply_fn = self._registry.pop_active_reply()
            if reply_fn and result:
                try:
                    await reply_fn(result)
                except Exception:
                    logger.exception("HeartbeatTask: platform reply failed")


__all__ = ["HeartbeatTask"]

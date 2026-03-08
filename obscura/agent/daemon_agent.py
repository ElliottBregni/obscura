"""DaemonAgent — event-driven always-on agent.

Stays alive indefinitely, reacting to **triggers** rather than a
conversational prompt loop.  Trigger kinds include scheduled (cron),
file-watch, memory-change, and peer-message events.

Usage::

    from obscura.agent.daemon_agent import DaemonAgent, Trigger, ScheduleTrigger

    agent = DaemonAgent(
        client,
        name="health-monitor",
        triggers=[
            ScheduleTrigger(
                cron="*/5 * * * *",
                prompt="Check system health and report anomalies",
                notify_user=True,
                priority=AttentionPriority.HIGH,
            ),
        ],
    )
    await agent.run_forever()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from obscura.agent.interaction import (
    AgentOutput,
    AttentionPriority,
    InteractionBus,
    UserResponse,
)
from obscura.core.types import AgentEventKind

if TYPE_CHECKING:
    from obscura.core.client import ObscuraClient

__all__ = [
    "DaemonAgent",
    "Trigger",
    "ScheduleTrigger",
    "FileWatchTrigger",
    "MemoryChangeTrigger",
    "PeerMessageTrigger",
    "IMessageTrigger",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trigger types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """Base trigger definition.

    All triggers carry a ``kind`` discriminator, an optional ``prompt``
    that the daemon will send to the LLM when the trigger fires, and
    notification preferences.
    """

    kind: str  # "schedule", "file_watch", "memory_change", "peer_message", "manual"
    description: str = ""
    prompt: str = ""
    notify_user: bool = False
    priority: AttentionPriority = AttentionPriority.NORMAL
    data: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


@dataclass(frozen=True)
class ScheduleTrigger(Trigger):
    """Cron-based trigger.

    ``cron`` follows standard 5-field cron syntax (minute hour dom month dow).
    The daemon evaluates it every minute.
    """

    kind: str = "schedule"
    cron: str = "* * * * *"


@dataclass(frozen=True)
class FileWatchTrigger(Trigger):
    """Fires when a file matching ``glob`` changes under ``path``."""

    kind: str = "file_watch"
    path: str = "."
    glob: str = "*"


@dataclass(frozen=True)
class MemoryChangeTrigger(Trigger):
    """Fires when a key matching ``key_pattern`` changes in ``namespace``."""

    kind: str = "memory_change"
    namespace: str = "default"
    key_pattern: str = "*"


@dataclass(frozen=True)
class PeerMessageTrigger(Trigger):
    """Fires when a message arrives from a specific peer (or any peer)."""

    kind: str = "peer_message"
    from_agent: str = "*"  # "*" = any peer


@dataclass(frozen=True)
class IMessageTrigger(Trigger):
    """Fires when a new iMessage arrives from configured contacts."""

    kind: str = "imessage"
    contacts: tuple[str, ...] = ()  # phone numbers or emails
    poll_interval: int = 30  # seconds


# ---------------------------------------------------------------------------
# DaemonAgent
# ---------------------------------------------------------------------------


class DaemonAgent:
    """Event-driven agent that reacts to triggers.

    Unlike :class:`LoopAgent` which waits for user input, the daemon
    watches for external events and autonomously processes them.  Each
    trigger fires at most one LLM invocation (``run_loop_to_completion``).

    The trigger queue is public — external systems push
    :class:`Trigger` instances into it via :meth:`fire`.
    """

    def __init__(
        self,
        client: ObscuraClient,
        *,
        name: str = "daemon",
        agent_id: str = "",
        triggers: list[Trigger] | None = None,
        interaction_bus: InteractionBus | None = None,
        max_turns_per_trigger: int = 15,
    ) -> None:
        self._client = client
        self._name = name
        self._agent_id = agent_id or f"daemon-{uuid4().hex[:8]}"
        self._static_triggers = list(triggers) if triggers else []
        self._bus = interaction_bus
        self._max_turns = max_turns_per_trigger
        self._trigger_queue: asyncio.Queue[Trigger] = asyncio.Queue()
        self._stopped = False
        self._trigger_count = 0
        self._scheduler_task: asyncio.Task[None] | None = None
        # Multi-turn iMessage conversation state: sender -> list of {role, text}
        self._conversations: dict[str, list[dict[str, str]]] = {}
        self._conversation_timestamps: dict[str, float] = {}
        self._session_timeout: float = 3600.0  # 1 hour inactivity resets thread

    # -- Public properties ---------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def trigger_count(self) -> int:
        """Number of triggers processed."""
        return self._trigger_count

    # -- Public API ----------------------------------------------------------

    async def fire(self, trigger: Trigger) -> None:
        """Enqueue a trigger for processing.

        Safe to call from any coroutine while the daemon is running.
        """
        await self._trigger_queue.put(trigger)

    async def run_forever(self) -> None:
        """Main loop: wait for triggers → process → notify → repeat.

        Blocks until :meth:`stop` is called.
        """
        logger.info("[%s] daemon agent started (id=%s)", self._name, self._agent_id)
        self._stopped = False

        # Start background schedulers for static triggers
        self._scheduler_task = asyncio.create_task(
            self._run_schedulers(),
        )

        try:
            logger.info("[%s] main loop entering", self._name)
            while not self._stopped:
                trigger = await self._get_next_trigger()
                if trigger is None:
                    logger.info("[%s] got None trigger, continuing", self._name)
                    continue

                logger.info(
                    "[%s] dequeued trigger #%d kind=%s",
                    self._name,
                    self._trigger_count,
                    trigger.kind,
                )

                try:
                    await self._handle_trigger(trigger)
                except Exception:
                    logger.exception(
                        "[%s] error handling trigger: %s",
                        self._name,
                        trigger.description or trigger.kind,
                    )
                    await self._emit_output(
                        f"[error] Failed to handle trigger: {trigger.description or trigger.kind}",
                        is_final=True,
                        event_kind=AgentEventKind.ERROR,
                    )

                self._trigger_count += 1

        except asyncio.CancelledError:
            logger.info("[%s] daemon agent cancelled", self._name)
        finally:
            self._stopped = True
            if self._scheduler_task and not self._scheduler_task.done():
                self._scheduler_task.cancel()
                try:
                    await self._scheduler_task
                except asyncio.CancelledError:
                    pass
            logger.info("[%s] daemon agent stopped", self._name)

    async def stop(self) -> None:
        """Signal the daemon to stop after the current trigger."""
        self._stopped = True
        # Unblock the trigger queue
        try:
            self._trigger_queue.put_nowait(
                Trigger(kind="__stop__"),
            )
        except asyncio.QueueFull:
            pass

    # -- Trigger handling (override in subclasses) ---------------------------

    async def _handle_trigger(self, trigger: Trigger) -> None:
        """Process a single trigger.  Subclasses can override this.

        The default implementation sends ``trigger.prompt`` through the
        LLM tool loop and optionally notifies the user.
        """
        if trigger.kind == "imessage":
            await self._handle_imessage_trigger(trigger)
            return

        prompt = trigger.prompt
        if not prompt:
            prompt = (
                f"A '{trigger.kind}' event occurred: {trigger.description}. "
                f"Data: {trigger.data}"
            )

        result = await self._client.run_loop_to_completion(
            prompt,
            max_turns=self._max_turns,
        )

        await self._emit_output(result, is_final=True)

        if trigger.notify_user:
            summary = result[:200] if len(result) > 200 else result
            await self._request_attention(
                f"Completed: {trigger.description or trigger.kind}\n\n{summary}",
                priority=trigger.priority,
            )

    async def _handle_imessage_trigger(self, trigger: Trigger) -> None:
        """Process an incoming iMessage: run agent loop, send reply (multi-turn)."""
        from obscura.integrations.imessage import IMessageClient

        sender = trigger.data.get("sender", "unknown")
        text = trigger.data.get("text", "")
        logger.info("[%s] handling iMessage from %s: %s", self._name, sender, text[:50])

        # -- Conversation thread management ----------------------------------
        now = time.monotonic()
        last_ts = self._conversation_timestamps.get(sender, 0.0)
        if now - last_ts > self._session_timeout:
            # Stale or new thread — start fresh
            self._conversations[sender] = []
            logger.debug("[%s] started new iMessage thread for %s", self._name, sender)
        self._conversation_timestamps[sender] = now

        thread = self._conversations[sender]
        thread.append({"role": "user", "text": text})

        # -- Build prompt with history context --------------------------------
        history_lines: list[str] = []
        for turn in thread[:-1]:  # all but the latest (included in the prompt below)
            role_label = "Them" if turn["role"] == "user" else "You"
            history_lines.append(f"{role_label}: {turn['text']}")

        if history_lines:
            history_block = "\n".join(history_lines)
            prompt = (
                f"You are in a multi-turn iMessage conversation with {sender}.\n\n"
                f"Conversation so far:\n{history_block}\n\n"
                f"Their latest message:\n\"{text}\"\n\n"
                f"Write your reply message ONLY — just the text you want to send back. "
                f"Do NOT use any tools. Do NOT describe what you would do. "
                f"Do NOT mention tools, capabilities, or limitations. "
                f"Just write the actual reply message as plain text. "
                f"The system will automatically send it as an iMessage."
            )
        else:
            prompt = (
                f"You received an iMessage from {sender}:\n\n"
                f"\"{text}\"\n\n"
                f"Write your reply message ONLY — just the text you want to send back. "
                f"Do NOT use any tools. Do NOT describe what you would do. "
                f"Do NOT mention tools, capabilities, or limitations. "
                f"Just write the actual reply message as plain text. "
                f"The system will automatically send it as an iMessage."
            )

        # Fresh session per message — Copilot's session state machine gets
        # stuck after a completed stream() call, causing subsequent calls to
        # hang.  Conversation history is already in the prompt, so session
        # state isn't needed.
        logger.info("[%s] calling LLM for iMessage from %s", self._name, sender)
        try:
            await self._client.reset_session()
            result = await asyncio.wait_for(
                self._client.run_loop_to_completion(prompt, max_turns=1),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.error("[%s] LLM call timed out for message from %s", self._name, sender)
            await self._emit_output(
                f"[iMessage from {sender}]: {text}\n\n[Reply]: (timed out)",
                is_final=True,
            )
            return
        except Exception:
            logger.exception("[%s] LLM call failed for message from %s", self._name, sender)
            await self._emit_output(
                f"[iMessage from {sender}]: {text}\n\n[Reply]: (error)",
                is_final=True,
            )
            return
        logger.info("[%s] LLM returned for %s: %s", self._name, sender, result[:80])

        # Append assistant reply to thread
        thread.append({"role": "assistant", "text": result})
        self._conversation_timestamps[sender] = time.monotonic()

        # -- Send reply via iMessage -----------------------------------------
        all_contacts = [
            c
            for t in self._static_triggers
            if isinstance(t, IMessageTrigger)
            for c in t.contacts
        ]
        client = IMessageClient(all_contacts)
        sent = await client.send_message(sender, result)
        if not sent:
            logger.error("[%s] Failed to send iMessage reply to %s", self._name, sender)

        # Emit to InteractionBus (shows in CLI)
        thread_len = len([t for t in thread if t["role"] == "user"])
        await self._emit_output(
            f"[iMessage from {sender}] (turn {thread_len}): {text}\n\n[Reply]: {result}",
            is_final=True,
        )

        if trigger.notify_user:
            await self._request_attention(
                f"iMessage from {sender}: {text[:100]}\nReply: {result[:100]}",
                priority=trigger.priority,
            )

    # -- Internal helpers ----------------------------------------------------

    async def _get_next_trigger(self) -> Trigger | None:
        """Block until a trigger arrives, or return ``None`` if stopped."""
        while not self._stopped:
            try:
                trigger = self._trigger_queue.get_nowait()
                if trigger.kind == "__stop__":
                    return None
                return trigger
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.5)
        return None

    async def _run_schedulers(self) -> None:
        """Background task that evaluates cron triggers and polls iMessage."""
        tasks: list[asyncio.Task[None]] = []

        schedule_triggers = [
            t for t in self._static_triggers if isinstance(t, ScheduleTrigger)
        ]
        if schedule_triggers:
            tasks.append(asyncio.create_task(self._poll_schedules(schedule_triggers)))

        imessage_triggers = [
            t for t in self._static_triggers if isinstance(t, IMessageTrigger)
        ]
        if imessage_triggers:
            tasks.append(asyncio.create_task(self._poll_imessages(imessage_triggers)))

        if not tasks:
            return

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_schedules(self, triggers: list[ScheduleTrigger]) -> None:
        """Evaluate cron triggers every 60 s."""
        while not self._stopped:
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                return

            for trigger in triggers:
                if self._stopped:
                    return
                if _cron_matches_now(trigger.cron):
                    logger.debug(
                        "[%s] schedule trigger fired: %s",
                        self._name,
                        trigger.description or trigger.cron,
                    )
                    await self._trigger_queue.put(trigger)

    async def _poll_imessages(self, triggers: list[IMessageTrigger]) -> None:
        """Poll iMessage for new messages from configured contacts."""
        from obscura.integrations.imessage import IMessageClient, IMessageState

        # Merge contacts and use smallest interval
        all_contacts = list({c for t in triggers for c in t.contacts})
        interval = min(t.poll_interval for t in triggers)

        client = IMessageClient(all_contacts)
        await client.check_access()

        state = IMessageState()
        if state.last_rowid == 0:
            state.initialize_from_db(client.db_path)

        # Track seen GUIDs to deduplicate (essential for AppleScript fallback
        # which doesn't filter by since_rowid)
        seen_guids: set[str] = set()

        logger.info(
            "[%s] iMessage polling started: contacts=%s interval=%ds",
            self._name,
            all_contacts,
            interval,
        )

        while not self._stopped:
            try:
                await asyncio.sleep(float(interval))
            except asyncio.CancelledError:
                return

            if self._stopped:
                return

            try:
                messages = await client.poll_unread(state.last_rowid)
                if messages:
                    logger.info(
                        "[%s] polled %d message(s), last_rowid=%d, seen=%d",
                        self._name, len(messages), state.last_rowid, len(seen_guids),
                    )
            except Exception:
                logger.exception("[%s] iMessage poll failed", self._name)
                continue

            for msg in messages:
                if msg.guid in seen_guids:
                    continue
                seen_guids.add(msg.guid)
                state.update(msg.rowid)
                logger.info(
                    "[%s] new message from %s: rowid=%d guid=%s",
                    self._name, msg.sender, msg.rowid, msg.guid[:12],
                )
                # Find matching trigger for notification prefs
                matching = triggers[0]
                for t in triggers:
                    if msg.sender in t.contacts:
                        matching = t
                        break

                fire_trigger = Trigger(
                    kind="imessage",
                    description=f"iMessage from {msg.sender}",
                    notify_user=matching.notify_user,
                    priority=matching.priority,
                    data={
                        "sender": msg.sender,
                        "text": msg.text,
                        "guid": msg.guid,
                        "date": msg.date.isoformat(),
                    },
                )
                await self._trigger_queue.put(fire_trigger)

    async def _emit_output(
        self,
        text: str,
        *,
        is_final: bool = False,
        event_kind: AgentEventKind | None = None,
    ) -> None:
        """Push output through the InteractionBus (if wired)."""
        if self._bus is None:
            return
        output = AgentOutput(
            agent_id=self._agent_id,
            agent_name=self._name,
            text=text,
            event_kind=event_kind,
            is_final=is_final,
        )
        try:
            await self._bus.emit_output(output)
        except Exception:
            logger.exception("[%s] failed to emit output", self._name)

    async def _request_attention(
        self,
        message: str,
        *,
        priority: AttentionPriority = AttentionPriority.NORMAL,
        actions: tuple[str, ...] | list[str] | None = None,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> UserResponse | None:
        """Ask for user attention via the InteractionBus."""
        if self._bus is None:
            logger.debug(
                "[%s] no interaction bus — skipping attention request", self._name
            )
            return None

        try:
            return await self._bus.request_attention(
                agent_id=self._agent_id,
                agent_name=self._name,
                message=message,
                priority=priority,
                actions=actions,
                context=context,
                timeout=timeout,
            )
        except Exception:
            logger.exception("[%s] attention request failed", self._name)
            return None


# ---------------------------------------------------------------------------
# Minimal cron matcher (minute hour dom month dow)
# ---------------------------------------------------------------------------


def _cron_matches_now(cron_expr: str) -> bool:
    """Return ``True`` if the 5-field cron expression matches the current minute.

    Supports ``*``, integer literals, and ``*/N`` step syntax.
    Does **not** support ranges or comma lists (add later if needed).
    """
    from datetime import datetime, timezone

    fields = cron_expr.strip().split()
    if len(fields) != 5:
        logger.warning("Invalid cron expression (expected 5 fields): %s", cron_expr)
        return False

    now = datetime.now(timezone.utc)
    now_values = (now.minute, now.hour, now.day, now.month, now.weekday())
    # cron weekday: 0=Sunday, Python weekday: 0=Monday
    # Remap: python_weekday → cron_weekday
    cron_dow = (now.weekday() + 1) % 7  # Mon=1..Sun=0
    now_values = (now.minute, now.hour, now.day, now.month, cron_dow)

    for field_str, now_val in zip(fields, now_values, strict=True):
        if field_str == "*":
            continue
        if field_str.startswith("*/"):
            try:
                step = int(field_str[2:])
                if step <= 0 or now_val % step != 0:
                    return False
            except ValueError:
                return False
        else:
            try:
                if int(field_str) != now_val:
                    return False
            except ValueError:
                return False

    return True

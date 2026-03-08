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
from obscura.integrations.messaging.identity import (
    build_conversation_key,
    normalize_identity,
)
from obscura.integrations.messaging.store import (
    ConversationStore,
    DaemonLockStore,
    MessageDedupeStore,
    MessageRuntimeEventStore,
    MessageSendEventStore,
)

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
    "MessageTrigger",
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


@dataclass(frozen=True)
class MessageTrigger(Trigger):
    """Generic message-platform trigger (platform adapter driven)."""

    kind: str = "message"
    platform: str = "imessage"
    contacts: tuple[str, ...] = ()  # identity strings for direct messages
    poll_interval: int = 30  # seconds
    account_id: str = "default"


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
        self._conversation_store = ConversationStore()
        self._dedupe_store = MessageDedupeStore()
        self._lock_store = DaemonLockStore()
        self._send_event_store = MessageSendEventStore()
        self._runtime_event_store = MessageRuntimeEventStore()
        self._session_timeout: float = 3600.0  # 1 hour inactivity resets thread
        self._trigger_timeout_s: float = 90.0  # LLM 60s + send 15s + 15s buffer
        self._heartbeat_interval_s: float = 15.0
        self._last_heartbeat_monotonic: float = 0.0
        self._lock_name = f"daemon:{self._name}"
        self._lock_owner = f"{self._agent_id}:{id(self)}"
        self._lock_stale_after_s: float = 300.0
        self._lock_retry_interval_s: float = 5.0
        self._lock_heartbeat_interval_s: float = 10.0
        self._last_lock_heartbeat_monotonic: float = 0.0

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
        self._record_runtime_event(
            "trigger_enqueued",
            platform=str(trigger.data.get("platform", "")),
            conversation_key=str(trigger.data.get("conversation_key", "")),
            message_id=str(trigger.data.get("message_id", "")),
            details={"kind": trigger.kind, "queue_size": self._trigger_queue.qsize()},
        )

    def _record_runtime_event(
        self,
        event_type: str,
        *,
        platform: str = "",
        conversation_key: str = "",
        message_id: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            self._runtime_event_store.add(
                component=self._name,
                event_type=event_type,
                platform=platform,
                conversation_key=conversation_key,
                message_id=message_id,
                details=details,
            )
        except Exception:
            logger.exception("[%s] failed to persist runtime event: %s", self._name, event_type)

    async def run_forever(self) -> None:
        """Main loop: wait for triggers → process → notify → repeat.

        Blocks until :meth:`stop` is called.
        """
        logger.info("[%s] daemon agent started (id=%s)", self._name, self._agent_id)
        self._stopped = False

        while not self._stopped:
            if self._lock_store.try_acquire(
                lock_name=self._lock_name,
                owner_id=self._lock_owner,
                stale_after_s=self._lock_stale_after_s,
            ):
                break
            logger.warning(
                "[%s] another daemon instance owns lock '%s'; waiting %.1fs",
                self._name,
                self._lock_name,
                self._lock_retry_interval_s,
            )
            self._record_runtime_event(
                "daemon_lock_wait",
                details={"lock_name": self._lock_name},
            )
            try:
                await asyncio.sleep(self._lock_retry_interval_s)
            except asyncio.CancelledError:
                self._stopped = True
                break

        if self._stopped:
            return
        self._record_runtime_event("daemon_lock_acquired", details={"lock_name": self._lock_name})

        # Start background schedulers for static triggers
        self._scheduler_task = asyncio.create_task(self._run_schedulers())

        try:
            logger.info("[%s] main loop entering", self._name)
            while not self._stopped:
                # Watchdog: if poll/scheduler task dies unexpectedly, restart it.
                if self._scheduler_task and self._scheduler_task.done() and not self._stopped:
                    try:
                        exc = self._scheduler_task.exception()
                    except asyncio.CancelledError:
                        exc = None
                    if exc is not None:
                        logger.error(
                            "[%s] scheduler task crashed; restarting: %s",
                            self._name,
                            exc,
                        )
                        self._record_runtime_event(
                            "scheduler_restarted",
                            details={"reason": "crash", "error": str(exc)},
                        )
                    else:
                        logger.warning("[%s] scheduler task stopped; restarting", self._name)
                        self._record_runtime_event(
                            "scheduler_restarted",
                            details={"reason": "stopped"},
                        )
                    self._scheduler_task = asyncio.create_task(self._run_schedulers())

                trigger = await self._get_next_trigger()
                if trigger is None:
                    now = time.monotonic()
                    if (
                        now - self._last_lock_heartbeat_monotonic
                        >= self._lock_heartbeat_interval_s
                    ):
                        self._last_lock_heartbeat_monotonic = now
                        if not self._lock_store.heartbeat(
                            lock_name=self._lock_name,
                            owner_id=self._lock_owner,
                        ):
                            logger.error(
                                "[%s] daemon lock lost for '%s'; reacquiring",
                                self._name,
                                self._lock_name,
                            )
                            self._record_runtime_event(
                                "daemon_lock_lost",
                                details={"lock_name": self._lock_name},
                            )
                            while not self._stopped:
                                if self._lock_store.try_acquire(
                                    lock_name=self._lock_name,
                                    owner_id=self._lock_owner,
                                    stale_after_s=self._lock_stale_after_s,
                                ):
                                    self._record_runtime_event(
                                        "daemon_lock_reacquired",
                                        details={"lock_name": self._lock_name},
                                    )
                                    break
                                await asyncio.sleep(self._lock_retry_interval_s)
                    if now - self._last_heartbeat_monotonic >= self._heartbeat_interval_s:
                        self._last_heartbeat_monotonic = now
                        self._record_runtime_event(
                            "daemon_heartbeat",
                            details={
                                "queue_size": self._trigger_queue.qsize(),
                                "trigger_count": self._trigger_count,
                            },
                        )
                    continue

                self._record_runtime_event(
                    "trigger_dequeued",
                    platform=str(trigger.data.get("platform", "")),
                    conversation_key=str(trigger.data.get("conversation_key", "")),
                    message_id=str(trigger.data.get("message_id", "")),
                    details={"kind": trigger.kind, "queue_size": self._trigger_queue.qsize()},
                )
                logger.info(
                    "[%s] dequeued trigger #%d kind=%s",
                    self._name,
                    self._trigger_count,
                    trigger.kind,
                )

                try:
                    await asyncio.wait_for(
                        self._handle_trigger(trigger),
                        timeout=self._trigger_timeout_s,
                    )
                except asyncio.TimeoutError:
                    self._record_runtime_event(
                        "trigger_timeout",
                        platform=str(trigger.data.get("platform", "")),
                        conversation_key=str(trigger.data.get("conversation_key", "")),
                        message_id=str(trigger.data.get("message_id", "")),
                        details={"kind": trigger.kind, "timeout_s": self._trigger_timeout_s},
                    )
                    logger.error(
                        "[%s] trigger timeout kind=%s after %.1fs",
                        self._name,
                        trigger.kind,
                        self._trigger_timeout_s,
                    )
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
                    self._record_runtime_event(
                        "trigger_error",
                        platform=str(trigger.data.get("platform", "")),
                        conversation_key=str(trigger.data.get("conversation_key", "")),
                        message_id=str(trigger.data.get("message_id", "")),
                        details={"kind": trigger.kind},
                    )

                self._trigger_count += 1

        except asyncio.CancelledError:
            logger.info("[%s] daemon agent cancelled", self._name)
        finally:
            self._stopped = True
            if self._scheduler_task:
                if not self._scheduler_task.done():
                    self._scheduler_task.cancel()
                    try:
                        await self._scheduler_task
                    except asyncio.CancelledError:
                        pass
                else:
                    try:
                        exc = self._scheduler_task.exception()
                        if exc is not None:
                            logger.error(
                                "[%s] scheduler task ended with error during shutdown: %s",
                                self._name,
                                exc,
                            )
                    except asyncio.CancelledError:
                        pass
            logger.info("[%s] daemon agent stopped", self._name)
            self._lock_store.release(lock_name=self._lock_name, owner_id=self._lock_owner)

    async def loop_forever(self) -> None:
        """Backward-compatible alias for callers expecting ``loop_forever``."""
        await self.run_forever()

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
        if trigger.kind in {"imessage", "message"}:
            await self._handle_message_trigger(trigger)
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

    async def _handle_message_trigger(self, trigger: Trigger) -> None:
        """Process incoming platform message and send a reply (multi-turn)."""
        from obscura.integrations.messaging.factory import get_adapter

        platform = str(trigger.data.get("platform", "imessage"))
        account_id = str(trigger.data.get("account_id", "default"))
        sender = trigger.data.get("sender", "unknown")
        sender_display = trigger.data.get("sender_display") or sender
        sender_id = trigger.data.get("sender_id") or normalize_identity(sender)
        sender_target = str(trigger.data.get("sender_target") or sender)
        forced_recipient = trigger.data.get("forced_recipient")
        if forced_recipient:
            sender_target = str(forced_recipient)
        conversation_key = trigger.data.get("conversation_key", "")
        if not conversation_key:
            conversation_key = build_conversation_key(
                platform=platform,
                account_id=account_id,
                channel_id=f"dm:{sender_id}",
                participants=["me", sender_id],
            )
        text = trigger.data.get("text", "")
        message_id = str(trigger.data.get("message_id", ""))
        self._record_runtime_event(
            "message_handle_start",
            platform=platform,
            conversation_key=str(conversation_key),
            message_id=message_id,
            details={"sender": str(sender_display), "sender_target": sender_target},
        )
        logger.info(
            "[%s] handling %s message from %s key=%s: %s",
            self._name,
            platform,
            sender_display,
            conversation_key[:12],
            text[:50],
        )

        # -- Conversation thread management ----------------------------------
        thread = self._conversation_store.ensure(
            conversation_key=conversation_key,
            platform=platform,
            account_id=account_id,
            channel_id=f"dm:{sender_id}",
            participants=["me", sender_id],
        )
        was_reset = self._conversation_store.reset_if_stale(
            conversation_key,
            timeout_seconds=self._session_timeout,
        )
        if was_reset:
            logger.debug(
                "[%s] reset stale %s conversation key=%s",
                self._name,
                platform,
                conversation_key[:12],
            )
        thread = self._conversation_store.append_user_message(conversation_key, text)
        logger.debug(
            "[%s] %s thread key=%s turns=%d",
            self._name,
            platform,
            conversation_key[:12],
            self._conversation_store.user_turn_count(thread),
        )

        # -- Build prompt with history context --------------------------------
        _MAX_HISTORY_TURNS = 20  # cap to avoid token bloat
        recent_history = thread.history[:-1]  # all but the latest
        # Drop trailing empty assistant turns (caused by LLM failures)
        while recent_history and recent_history[-1].get("text", "").strip() == "":
            recent_history = recent_history[:-1]
        # Cap to last N turns
        recent_history = recent_history[-_MAX_HISTORY_TURNS:]
        history_lines: list[str] = []
        for turn in recent_history:
            if not turn.get("text", "").strip():
                continue  # skip empty turns
            role_label = "Them" if turn["role"] == "user" else "You"
            history_lines.append(f"{role_label}: {turn['text']}")

        if history_lines:
            history_block = "\n".join(history_lines)
            prompt = (
                f"You are in a multi-turn {platform} conversation with {sender_display}.\n\n"
                f"Conversation so far:\n{history_block}\n\n"
                f"Their latest message:\n\"{text}\"\n\n"
                f"Write your reply message ONLY — just the text you want to send back. "
                f"Do NOT use any tools. Do NOT describe what you would do. "
                f"Do NOT mention tools, capabilities, or limitations. "
                f"Just write the actual reply message as plain text. "
                f"The system will automatically send it as a message."
            )
        else:
            prompt = (
                f"You received a {platform} message from {sender_display}:\n\n"
                f"\"{text}\"\n\n"
                f"Write your reply message ONLY — just the text you want to send back. "
                f"Do NOT use any tools. Do NOT describe what you would do. "
                f"Do NOT mention tools, capabilities, or limitations. "
                f"Just write the actual reply message as plain text. "
                f"The system will automatically send it as a message."
            )

        # Fresh session per message — Copilot's session state machine gets
        # stuck after a completed stream() call, causing subsequent calls to
        # hang.  Conversation history is already in the prompt, so session
        # state isn't needed.
        logger.info("[%s] calling LLM for %s message from %s", self._name, platform, sender_display)
        try:
            await self._client.reset_session()
            result = await asyncio.wait_for(
                self._client.run_loop_to_completion(prompt, max_turns=self._max_turns),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] LLM call timed out for message from %s",
                self._name,
                sender_display,
            )
            await self._emit_output(
                f"[{platform} from {sender_display}]: {text}\n\n[Reply]: (timed out)",
                is_final=True,
            )
            self._record_runtime_event(
                "llm_timeout",
                platform=platform,
                conversation_key=str(conversation_key),
                message_id=message_id,
            )
            return
        except Exception:
            logger.exception(
                "[%s] LLM call failed for message from %s",
                self._name,
                sender_display,
            )
            await self._emit_output(
                f"[{platform} from {sender_display}]: {text}\n\n[Reply]: (error)",
                is_final=True,
            )
            self._record_runtime_event(
                "llm_error",
                platform=platform,
                conversation_key=str(conversation_key),
                message_id=message_id,
            )
            return
        logger.info("[%s] LLM returned for %s: %s", self._name, sender_display, result[:80])
        self._record_runtime_event(
            "llm_ok",
            platform=platform,
            conversation_key=str(conversation_key),
            message_id=message_id,
            details={"reply_preview": result[:120]},
        )

        # Guard: skip empty replies (LLM returned nothing useful)
        if not result.strip():
            logger.warning(
                "[%s] LLM returned empty reply for %s message from %s; skipping send",
                self._name,
                platform,
                sender_display,
            )
            self._record_runtime_event(
                "llm_empty_reply",
                platform=platform,
                conversation_key=str(conversation_key),
                message_id=message_id,
            )
            return

        # Append assistant reply to thread
        thread = self._conversation_store.append_assistant_message(conversation_key, result)

        # -- Send reply via platform adapter ---------------------------------
        all_contacts = list(
            {
                c
                for t in self._static_triggers
                if (
                    (isinstance(t, IMessageTrigger) and platform == "imessage")
                    or (isinstance(t, MessageTrigger) and t.platform == platform)
                )
                for c in t.contacts
            }
        )
        adapter = get_adapter(
            platform=platform,
            contacts=all_contacts or [sender_target],
            account_id=account_id,
        )
        allowed_norm = {normalize_identity(c) for c in all_contacts if c}
        target_norm = normalize_identity(sender_target)
        if allowed_norm and target_norm not in allowed_norm:
            err = f"blocked_recipient:{sender_target}"
            logger.error(
                "[%s] blocked %s reply to %s (allowed=%s)",
                self._name,
                platform,
                sender_target,
                sorted(allowed_norm),
            )
            self._send_event_store.add(
                platform=platform,
                conversation_key=conversation_key,
                recipient=sender_target,
                success=False,
                error_text=err,
                reply_text=result,
            )
            self._record_runtime_event(
                "send_blocked_recipient",
                platform=platform,
                conversation_key=str(conversation_key),
                message_id=message_id,
                details={"recipient": sender_target, "allowed": sorted(allowed_norm)},
            )
            return

        send_error = ""
        self._record_runtime_event(
            "send_attempt",
            platform=platform,
            conversation_key=str(conversation_key),
            message_id=message_id,
            details={"recipient": sender_target},
        )
        try:
            sent = await asyncio.wait_for(
                adapter.send(sender_target, result),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] send timed out for %s reply to %s (target=%s)",
                self._name,
                platform,
                sender_display,
                sender_target,
            )
            sent = False
            send_error = "send_timeout"
        if not sent:
            if not send_error:
                send_error = "send_failed"
            logger.error(
                "[%s] Failed to send %s reply to %s (target=%s)",
                self._name,
                platform,
                sender_display,
                sender_target,
            )
            self._record_runtime_event(
                "send_failed",
                platform=platform,
                conversation_key=str(conversation_key),
                message_id=message_id,
                details={"recipient": sender_target, "error": send_error},
            )
        else:
            self._record_runtime_event(
                "send_ok",
                platform=platform,
                conversation_key=str(conversation_key),
                message_id=message_id,
                details={"recipient": sender_target},
            )
        self._send_event_store.add(
            platform=platform,
            conversation_key=conversation_key,
            recipient=sender_target,
            success=sent,
            error_text=send_error,
            reply_text=result,
        )

        # Emit to InteractionBus (shows in CLI)
        thread_len = self._conversation_store.user_turn_count(thread)
        await self._emit_output(
            f"[{platform} from {sender_display}] (turn {thread_len}): {text}\n\n[Reply]: {result}",
            is_final=True,
        )

        if trigger.notify_user:
            await self._request_attention(
                f"{platform} from {sender_display}: {text[:100]}\nReply: {result[:100]}",
                priority=trigger.priority,
            )

    async def _handle_imessage_trigger(self, trigger: Trigger) -> None:
        """Backward-compatible alias for older callers."""
        await self._handle_message_trigger(trigger)

    # -- Internal helpers ----------------------------------------------------

    async def _get_next_trigger(self) -> Trigger | None:
        """Block until a trigger arrives, or return ``None`` if stopped."""
        while not self._stopped:
            try:
                trigger = await asyncio.wait_for(self._trigger_queue.get(), timeout=0.5)
                if trigger.kind == "__stop__":
                    return None
                return trigger
            except asyncio.TimeoutError:
                return None
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

        message_triggers = [
            t for t in self._static_triggers if isinstance(t, MessageTrigger)
        ]
        if message_triggers:
            tasks.append(asyncio.create_task(self._poll_messages(message_triggers)))

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
        generic = [
            MessageTrigger(
                platform="imessage",
                contacts=t.contacts,
                poll_interval=t.poll_interval,
                account_id="default",
                prompt=t.prompt,
                description=t.description or "iMessage polling",
                notify_user=t.notify_user,
                priority=t.priority,
                data=dict(t.data),
            )
            for t in triggers
        ]
        await self._poll_messages(generic)

    async def _poll_messages(self, triggers: list[MessageTrigger]) -> None:
        """Poll generic message platforms via registered adapters."""
        from obscura.integrations.messaging.factory import get_adapter

        # Group by (platform, account_id, poll_interval) so each adapter is isolated.
        groups: dict[tuple[str, str, int], list[MessageTrigger]] = {}
        for trig in triggers:
            key = (trig.platform, trig.account_id, trig.poll_interval)
            groups.setdefault(key, []).append(trig)

        adapters: list[tuple[str, str, int, Any, list[MessageTrigger]]] = []
        next_due: dict[tuple[str, str, int], float] = {}
        for (platform, account_id, interval), tgroup in groups.items():
            contacts = list({c for t in tgroup for c in t.contacts})
            adapter = get_adapter(
                platform=platform,
                contacts=contacts,
                account_id=account_id,
            )
            await adapter.start()
            self._record_runtime_event(
                "poll_adapter_started",
                platform=platform,
                details={"account_id": account_id, "interval": interval, "contacts": contacts},
            )
            adapters.append((platform, account_id, interval, adapter, tgroup))
            next_due[(platform, account_id, interval)] = 0.0
            logger.info(
                "[%s] message polling started: platform=%s account=%s contacts=%s interval=%ds",
                self._name,
                platform,
                account_id,
                contacts,
                interval,
            )

        while not self._stopped:
            now = asyncio.get_running_loop().time()
            for platform, account_id, interval, adapter, tgroup in adapters:
                group_key = (platform, account_id, interval)
                if now < next_due[group_key]:
                    continue
                next_due[group_key] = now + max(1, interval)
                if self._stopped:
                    return
                try:
                    messages = await adapter.poll()
                except Exception:
                    logger.exception(
                        "[%s] message poll failed: platform=%s account=%s",
                        self._name,
                        platform,
                        account_id,
                    )
                    self._record_runtime_event(
                        "poll_error",
                        platform=platform,
                        details={"account_id": account_id},
                    )
                    continue

                for msg in messages:
                    try:
                        dedupe_key = f"{msg.platform}:{msg.message_id}"
                        if not self._dedupe_store.add_if_absent(dedupe_key):
                            self._record_runtime_event(
                                "message_deduped",
                                platform=msg.platform,
                                message_id=msg.message_id,
                                details={"dedupe_key": dedupe_key},
                            )
                            continue

                        msg_sender_key = normalize_identity(msg.sender_id)
                        sender_display = str(msg.metadata.get("sender_raw", msg.sender_id))
                        sender_target = str(msg.metadata.get("sender_target", msg.sender_id))
                        conversation_key = build_conversation_key(
                            platform=msg.platform,
                            account_id=msg.account_id,
                            channel_id=msg.channel_id,
                            participants=[msg.recipient_id, msg_sender_key],
                        )
                        matching = tgroup[0]
                        for t in tgroup:
                            trigger_contact_keys = {normalize_identity(c) for c in t.contacts}
                            if msg_sender_key in trigger_contact_keys:
                                matching = t
                                break

                        fire_trigger = Trigger(
                            kind="imessage" if msg.platform == "imessage" else "message",
                            description=f"{msg.platform} message from {sender_display}",
                            notify_user=matching.notify_user,
                            priority=matching.priority,
                            data={
                                "platform": msg.platform,
                                "account_id": msg.account_id,
                                "channel_id": msg.channel_id,
                                "conversation_key": conversation_key,
                                "sender": sender_display,
                                "sender_id": msg_sender_key,
                                "sender_display": sender_display,
                                "sender_target": sender_target,
                                "text": msg.text,
                                "message_id": msg.message_id,
                                "date": msg.timestamp.isoformat(),
                            },
                        )
                        forced_recipient = matching.data.get("forced_recipient")
                        if forced_recipient:
                            fire_trigger.data["forced_recipient"] = str(forced_recipient)
                        await self._trigger_queue.put(fire_trigger)
                        qsize = self._trigger_queue.qsize()
                        if qsize >= 10:
                            self._record_runtime_event(
                                "queue_backpressure",
                                platform=msg.platform,
                                conversation_key=conversation_key,
                                message_id=msg.message_id,
                                details={"queue_size": qsize},
                            )
                        self._record_runtime_event(
                            "message_enqueued",
                            platform=msg.platform,
                            conversation_key=conversation_key,
                            message_id=msg.message_id,
                            details={
                                "sender": sender_display,
                                "sender_id": msg_sender_key,
                                "sender_target": sender_target,
                            },
                        )
                    except Exception:
                        logger.exception(
                            "[%s] failed to process inbound message id=%s platform=%s",
                            self._name,
                            msg.message_id,
                            msg.platform,
                        )
                        self._record_runtime_event(
                            "message_process_error",
                            platform=msg.platform,
                            message_id=msg.message_id,
                        )
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return

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

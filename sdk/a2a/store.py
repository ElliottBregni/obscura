"""
sdk.a2a.store — Task persistence with state machine enforcement.

Provides ``InMemoryTaskStore`` for development/testing and
``RedisTaskStore`` for production (durable, pub/sub, multi-instance).

Both implement the same ``TaskStore`` protocol — the A2AService
doesn't care which backend is used.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from sdk.a2a.types import (
    A2AMessage,
    Artifact,
    InvalidTransitionError,
    StreamEvent,
    Task,
    TaskArtifactUpdateEvent,
    TaskNotFoundError,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TaskStore(Protocol):
    """Contract for A2A task persistence backends."""

    async def create_task(
        self, context_id: str, initial_message: A2AMessage
    ) -> Task: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def list_tasks(
        self,
        context_id: str | None = None,
        state: TaskState | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None]: ...

    async def transition(
        self,
        task_id: str,
        new_state: TaskState,
        message: A2AMessage | None = None,
    ) -> Task: ...

    async def add_artifact(self, task_id: str, artifact: Artifact) -> Task: ...

    async def append_message(self, task_id: str, message: A2AMessage) -> Task: ...

    async def cancel_task(self, task_id: str) -> Task: ...

    def subscribe(
        self, task_id: str
    ) -> AsyncIterator[StreamEvent]: ...

    async def publish_update(
        self, task_id: str, event: StreamEvent
    ) -> None: ...


# ---------------------------------------------------------------------------
# In-memory implementation (testing / development)
# ---------------------------------------------------------------------------


class InMemoryTaskStore:
    """In-memory task store with pub/sub via asyncio.Queue.

    Not durable — tasks lost on process restart. Used for testing
    and local development without Redis.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._context_tasks: dict[str, list[str]] = defaultdict(list)
        self._subscribers: dict[str, list[asyncio.Queue[StreamEvent]]] = defaultdict(list)

    async def create_task(
        self, context_id: str, initial_message: A2AMessage
    ) -> Task:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)

        task = Task(
            id=task_id,
            contextId=context_id,
            status=TaskStatus(state=TaskState.PENDING, timestamp=now),
            history=[initial_message],
        )

        self._tasks[task_id] = task
        self._context_tasks[context_id].append(task_id)
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list_tasks(
        self,
        context_id: str | None = None,
        state: TaskState | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None]:
        limit = max(1, min(limit, 100))

        # Collect candidates
        if context_id:
            task_ids = self._context_tasks.get(context_id, [])
            tasks = [self._tasks[tid] for tid in task_ids if tid in self._tasks]
        else:
            tasks = list(self._tasks.values())

        # Filter by state
        if state:
            tasks = [t for t in tasks if t.status.state == state]

        # Sort by timestamp descending (most recent first)
        tasks.sort(key=lambda t: t.status.timestamp, reverse=True)

        # Cursor-based pagination
        start_idx = 0
        if cursor:
            try:
                start_idx = int(base64.b64decode(cursor).decode())
            except Exception:
                start_idx = 0

        page = tasks[start_idx : start_idx + limit]
        next_cursor: str | None = None
        if start_idx + limit < len(tasks):
            next_cursor = base64.b64encode(str(start_idx + limit).encode()).decode()

        return page, next_cursor

    async def transition(
        self,
        task_id: str,
        new_state: TaskState,
        message: A2AMessage | None = None,
    ) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)

        current = task.status.state
        allowed = VALID_TRANSITIONS.get(current, frozenset())
        if new_state not in allowed:
            raise InvalidTransitionError(task_id, current.value, new_state.value)

        now = datetime.now(UTC)
        task.status = TaskStatus(
            state=new_state,
            message=message,
            timestamp=now,
        )

        if message:
            task.history.append(message)

        return task

    async def add_artifact(self, task_id: str, artifact: Artifact) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        task.artifacts.append(artifact)
        return task

    async def append_message(self, task_id: str, message: A2AMessage) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        task.history.append(message)
        return task

    async def cancel_task(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)

        current = task.status.state
        if current in TERMINAL_STATES:
            from sdk.a2a.types import TaskNotCancelableError

            raise TaskNotCancelableError(task_id, current.value)

        return await self.transition(task_id, TaskState.CANCELED)

    async def subscribe(self, task_id: str) -> AsyncIterator[StreamEvent]:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)

        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._subscribers[task_id].append(queue)

        try:
            while True:
                event = await queue.get()
                yield event

                # Stop on terminal status update
                if isinstance(event, TaskStatusUpdateEvent) and event.final:
                    break
        finally:
            self._subscribers[task_id].remove(queue)

    async def publish_update(self, task_id: str, event: StreamEvent) -> None:
        for queue in self._subscribers.get(task_id, []):
            await queue.put(event)


# ---------------------------------------------------------------------------
# Redis implementation (production)
# ---------------------------------------------------------------------------


class RedisTaskStore:
    """Redis-backed task store with pub/sub for real-time updates.

    Uses Redis hashes for task storage, sorted sets for context indexing,
    and pub/sub channels for streaming subscribers.

    Keys:
        a2a:task:{task_id}          — JSON-serialized Task
        a2a:context:{context_id}    — sorted set of task_ids (score = timestamp)
        a2a:updates:{task_id}       — pub/sub channel for streaming events
    """

    def __init__(self, redis_url: str, task_ttl: int = 86400) -> None:
        self._redis_url = redis_url
        self._task_ttl = task_ttl
        self._redis: Any = None  # redis.asyncio.Redis

    async def connect(self) -> None:
        """Connect to Redis."""
        try:
            import redis.asyncio as aioredis  # type: ignore[import-untyped]

            self._redis: Any = aioredis.from_url(  # pyright: ignore[reportUnknownMemberType]
                self._redis_url,
                decode_responses=True,
            )
            await self._redis.ping()  # pyright: ignore[reportUnknownMemberType]
            logger.info("Connected to Redis at %s", self._redis_url)
        except ImportError:
            raise ImportError(
                "redis package required for RedisTaskStore. "
                "Install with: pip install 'obscura[a2a]'"
            )

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    def _task_key(self, task_id: str) -> str:
        return f"a2a:task:{task_id}"

    def _context_key(self, context_id: str) -> str:
        return f"a2a:context:{context_id}"

    def _updates_channel(self, task_id: str) -> str:
        return f"a2a:updates:{task_id}"

    async def _save_task(self, task: Task) -> None:
        """Persist task to Redis with optional TTL."""
        key = self._task_key(task.id)
        data = task.model_dump_json()
        if task.status.state in TERMINAL_STATES:
            await self._redis.setex(key, self._task_ttl, data)
        else:
            await self._redis.set(key, data)

    async def _load_task(self, task_id: str) -> Task | None:
        """Load task from Redis."""
        data = await self._redis.get(self._task_key(task_id))
        if data is None:
            return None
        return Task.model_validate_json(data)

    async def create_task(
        self, context_id: str, initial_message: A2AMessage
    ) -> Task:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)

        task = Task(
            id=task_id,
            contextId=context_id,
            status=TaskStatus(state=TaskState.PENDING, timestamp=now),
            history=[initial_message],
        )

        await self._save_task(task)

        # Index under context
        score = now.timestamp()
        await self._redis.zadd(self._context_key(context_id), {task_id: score})

        return task

    async def get_task(self, task_id: str) -> Task | None:
        return await self._load_task(task_id)

    async def list_tasks(
        self,
        context_id: str | None = None,
        state: TaskState | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None]:
        limit = max(1, min(limit, 100))

        start_idx = 0
        if cursor:
            try:
                start_idx = int(base64.b64decode(cursor).decode())
            except Exception:
                start_idx = 0

        task_ids: list[str]
        if context_id:
            # Get task IDs from context sorted set (reverse chronological)
            raw_ids: list[Any] = await self._redis.zrevrange(
                self._context_key(context_id), 0, -1
            )
            task_ids = [str(x) for x in raw_ids]
        else:
            # Scan for all task keys
            task_ids = []
            async for key in self._redis.scan_iter(match="a2a:task:*"):
                task_ids.append(str(key).replace("a2a:task:", ""))

        # Load tasks
        tasks: list[Task] = []
        for tid in task_ids:
            task = await self._load_task(tid)
            if task is None:
                continue
            if state and task.status.state != state:
                continue
            tasks.append(task)

        # Paginate
        page = tasks[start_idx : start_idx + limit]
        next_cursor: str | None = None
        if start_idx + limit < len(tasks):
            next_cursor = base64.b64encode(str(start_idx + limit).encode()).decode()

        return page, next_cursor

    async def transition(
        self,
        task_id: str,
        new_state: TaskState,
        message: A2AMessage | None = None,
    ) -> Task:
        task = await self._load_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)

        current = task.status.state
        allowed = VALID_TRANSITIONS.get(current, frozenset())
        if new_state not in allowed:
            raise InvalidTransitionError(task_id, current.value, new_state.value)

        now = datetime.now(UTC)
        task.status = TaskStatus(state=new_state, message=message, timestamp=now)

        if message:
            task.history.append(message)

        await self._save_task(task)
        return task

    async def add_artifact(self, task_id: str, artifact: Artifact) -> Task:
        task = await self._load_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        task.artifacts.append(artifact)
        await self._save_task(task)
        return task

    async def append_message(self, task_id: str, message: A2AMessage) -> Task:
        task = await self._load_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        task.history.append(message)
        await self._save_task(task)
        return task

    async def cancel_task(self, task_id: str) -> Task:
        task = await self._load_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        current = task.status.state
        if current in TERMINAL_STATES:
            from sdk.a2a.types import TaskNotCancelableError

            raise TaskNotCancelableError(task_id, current.value)
        return await self.transition(task_id, TaskState.CANCELED)

    async def subscribe(self, task_id: str) -> AsyncIterator[StreamEvent]:
        task = await self._load_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)

        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._updates_channel(task_id))

        try:
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                data = json.loads(msg["data"])
                kind = data.get("kind")
                if kind == "status-update":
                    event = TaskStatusUpdateEvent.model_validate(data)
                    yield event
                    if event.final:
                        break
                elif kind == "artifact-update":
                    yield TaskArtifactUpdateEvent.model_validate(data)
        finally:
            await pubsub.unsubscribe(self._updates_channel(task_id))
            await pubsub.aclose()

    async def publish_update(self, task_id: str, event: StreamEvent) -> None:
        if self._redis:
            await self._redis.publish(
                self._updates_channel(task_id),
                event.model_dump_json(),
            )

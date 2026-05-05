"""Eval memory — store eval results in vector memory for RAG retrieval.

Eval failures, tool quality signals, and session outcomes are stored as
searchable vector memories.  Before tool calls and at turn start, relevant
past failures are retrieved and injected into agent context so models
learn from mistakes without repeating them.

Fixes over v1:
- #1  Context-aware recall — only inject failures relevant to the current
      tool/file, not random recent failures.
- #2  TTL + decay — memories older than ``MAX_AGE_SECONDS`` are filtered
      out at query time and pruned periodically.
- #3  Resolution tracking — when a tool call succeeds on a file that
      previously failed, a "resolved" entry is stored that suppresses
      the old failure from recall results.
- #5  Criteria feedback — track grading false-positive rates per command
      so criteria can be tuned.
"""

from __future__ import annotations

import logging
import time
from typing import Any, cast

logger = logging.getLogger(__name__)

# Namespace for all eval memories
_EVAL_NAMESPACE = "eval:results"

# Memories older than this are filtered from recall (7 days)
MAX_AGE_SECONDS = 7 * 24 * 3600

# Minimum similarity score for recall results
_RECALL_THRESHOLD = 0.4


class EvalMemory:
    """Stores and retrieves eval results via vector memory."""

    _instance: EvalMemory | None = None

    def __init__(self) -> None:
        self._store: Any = None
        self._available = False
        # In-memory cache of resolved file+tool pairs to suppress stale failures
        self._resolved: dict[str, float] = {}  # "tool:file" → resolved_at timestamp
        # Criteria feedback: track pass/fail per command for false-positive detection
        self._criteria_stats: dict[
            str,
            dict[str, int],
        ] = {}  # cmd → {criterion → fail_count}
        self._init_store()

    def _init_store(self) -> None:
        """Try to initialize the vector memory store."""
        try:
            from obscura.auth.models import AuthenticatedUser
            from obscura.vector_memory import VectorMemoryStore

            user = AuthenticatedUser(
                user_id="system:eval",
                email="eval-system@obscura.local",
                roles=("system",),
                org_id=None,
                token_type="service",
                raw_token="",
            )
            self._store = VectorMemoryStore.for_user(user)
            self._available = True
        except Exception as exc:
            logger.debug("Eval memory not available: %s", exc)
            self._available = False

    @classmethod
    def get_instance(cls) -> EvalMemory:
        """Get or create the singleton eval memory instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # Resolution tracking (#3)
    # ------------------------------------------------------------------

    def _resolution_key(self, tool_name: str, file_path: str) -> str:
        return f"{tool_name}:{file_path}"

    def _is_resolved(self, tool_name: str, file_path: str) -> bool:
        """Check if a tool+file failure has been resolved."""
        key = self._resolution_key(tool_name, file_path)
        resolved_at = self._resolved.get(key)
        if resolved_at is None:
            return False
        # Resolution is valid for MAX_AGE_SECONDS
        return (time.time() - resolved_at) < MAX_AGE_SECONDS

    # ------------------------------------------------------------------
    # Age filtering (#2)
    # ------------------------------------------------------------------

    def _is_fresh(self, metadata: dict[str, Any]) -> bool:
        """Check if a memory entry is within the TTL window."""
        ts = metadata.get("timestamp", 0)
        if not ts:
            return True  # no timestamp → don't filter
        age = time.time() - float(ts)
        return age < MAX_AGE_SECONDS

    # ------------------------------------------------------------------
    # Record events
    # ------------------------------------------------------------------

    def record_tool_failure(
        self,
        tool_name: str,
        error: str,
        *,
        file_path: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        """Store a tool eval failure for future retrieval."""
        if not self._available:
            return
        try:
            # Clear any existing resolution for this tool+file
            if file_path:
                key = self._resolution_key(tool_name, file_path)
                self._resolved.pop(key, None)

            store_key = f"tool-fail-{tool_name}-{int(time.time())}"
            text = (
                f"Tool '{tool_name}' failed eval check"
                + (f" on file {file_path}" if file_path else "")
                + f": {error}"
            )
            metadata: dict[str, Any] = {
                "type": "tool_failure",
                "tool_name": tool_name,
                "timestamp": time.time(),
                "resolved": False,
            }
            if file_path:
                metadata["file_path"] = file_path
            if context:
                metadata.update(context)

            self._store.set(
                key=store_key,
                text=text,
                metadata=metadata,
                namespace=_EVAL_NAMESPACE,
                memory_type="eval_failure",
            )
            logger.debug("Recorded tool failure: %s", store_key)
        except Exception as exc:
            logger.debug("Failed to record tool failure: %s", exc)

    def record_tool_success(
        self,
        tool_name: str,
        *,
        file_path: str = "",
    ) -> None:
        """Record that a tool call succeeded — resolves past failures (#3).

        When a tool+file pair that previously failed now succeeds, we mark
        it resolved so stale warnings stop appearing.
        """
        if file_path:
            key = self._resolution_key(tool_name, file_path)
            self._resolved[key] = time.time()

        if not self._available or not file_path:
            return
        try:
            store_key = f"tool-resolved-{tool_name}-{int(time.time())}"
            text = (
                f"Tool '{tool_name}' now passing on file {file_path} "
                f"(previously failed — issue resolved)"
            )
            self._store.set(
                key=store_key,
                text=text,
                metadata={
                    "type": "tool_resolution",
                    "tool_name": tool_name,
                    "file_path": file_path,
                    "timestamp": time.time(),
                    "resolved": True,
                },
                namespace=_EVAL_NAMESPACE,
                memory_type="eval_resolution",
            )
        except Exception:
            logger.debug("suppressed exception in record_tool_success", exc_info=True)

    def record_eval_result(
        self,
        eval_type: str,
        passed: bool,
        score: float,
        detail: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Store an eval result (command, turn, or session level)."""
        if not self._available:
            return
        try:
            status = "passed" if passed else "failed"
            store_key = f"eval-{eval_type}-{status}-{int(time.time())}"
            text = f"Eval {eval_type} {status} (score: {score:.2f}): {detail}"
            metadata: dict[str, Any] = {
                "type": f"eval_{status}",
                "eval_type": eval_type,
                "score": score,
                "passed": passed,
                "timestamp": time.time(),
            }
            if context:
                metadata.update(context)

            self._store.set(
                key=store_key,
                text=text,
                metadata=metadata,
                namespace=_EVAL_NAMESPACE,
                memory_type="eval_result",
            )
        except Exception as exc:
            logger.debug("Failed to record eval result: %s", exc)

    def record_session_failure(
        self,
        session_id: str,
        reason: str,
        *,
        lint_errors: dict[str, str] | None = None,
        tool_errors: list[str] | None = None,
    ) -> None:
        """Store a session eval gate failure."""
        if not self._available:
            return
        try:
            store_key = f"session-fail-{session_id}-{int(time.time())}"
            parts = [f"Session {session_id} blocked by eval gate: {reason}"]
            if lint_errors:
                files = ", ".join(lint_errors.keys())
                parts.append(f"Lint errors in: {files}")
            if tool_errors:
                parts.append(f"Tool errors: {'; '.join(tool_errors[:3])}")
            text = ". ".join(parts)

            metadata: dict[str, Any] = {
                "type": "session_failure",
                "session_id": session_id,
                "reason": reason,
                "timestamp": time.time(),
            }
            if lint_errors:
                metadata["lint_files"] = list(lint_errors.keys())

            self._store.set(
                key=store_key,
                text=text,
                metadata=metadata,
                namespace=_EVAL_NAMESPACE,
                memory_type="eval_failure",
            )
        except Exception as exc:
            logger.debug("Failed to record session failure: %s", exc)

    # ------------------------------------------------------------------
    # Criteria feedback (#5)
    # ------------------------------------------------------------------

    def record_criteria_result(
        self,
        command: str,
        criterion: str,
        passed: bool,
    ) -> None:
        """Track per-criterion pass/fail for false-positive detection."""
        if command not in self._criteria_stats:
            self._criteria_stats[command] = {}
        stats = self._criteria_stats[command]
        key_pass = f"{criterion}:pass"
        key_fail = f"{criterion}:fail"
        if passed:
            stats[key_pass] = stats.get(key_pass, 0) + 1
        else:
            stats[key_fail] = stats.get(key_fail, 0) + 1

    def get_criteria_false_positive_rate(
        self,
        command: str,
        criterion: str,
    ) -> float | None:
        """Return fail rate for a criterion (None if no data).

        A high fail rate (>0.8) with mostly-passing overall evals
        suggests the criterion is too strict (false positive).
        """
        stats = self._criteria_stats.get(command, {})
        passes = stats.get(f"{criterion}:pass", 0)
        fails = stats.get(f"{criterion}:fail", 0)
        total = passes + fails
        if total < 3:
            return None  # not enough data
        return fails / total

    def get_suspect_criteria(self, command: str, threshold: float = 0.8) -> list[str]:
        """Return criteria that fail more than *threshold* of the time."""
        stats = self._criteria_stats.get(command, {})
        suspects: list[str] = []
        seen: set[str] = set()
        for key in stats:
            criterion = key.rsplit(":", 1)[0]
            if criterion in seen:
                continue
            seen.add(criterion)
            rate = self.get_criteria_false_positive_rate(command, criterion)
            if rate is not None and rate >= threshold:
                suspects.append(criterion)
        return suspects

    # ------------------------------------------------------------------
    # Context-aware recall (#1) with freshness filtering (#2)
    # and resolution suppression (#3)
    # ------------------------------------------------------------------

    def recall_for_tool(
        self,
        tool_name: str,
        *,
        file_path: str = "",
        top_k: int = 3,
    ) -> list[str]:
        """Retrieve past eval failures relevant to a specific tool+file.

        Only returns failures that are:
        - Fresh (within MAX_AGE_SECONDS)
        - Not resolved (no subsequent success on the same tool+file)
        - Semantically relevant (above similarity threshold)
        """
        if not self._available:
            return []

        # If this tool+file was recently resolved, skip entirely
        if file_path and self._is_resolved(tool_name, file_path):
            return []

        try:
            query = f"tool {tool_name} eval failure"
            if file_path:
                query += f" on file {file_path}"

            results = self._store.search_similar(
                query=query,
                namespace=_EVAL_NAMESPACE,
                top_k=top_k * 2,  # fetch more, then filter
                memory_types=["eval_failure"],
                threshold=_RECALL_THRESHOLD,
            )
            filtered: list[str] = []
            for r in results:
                if r.score < _RECALL_THRESHOLD:
                    continue
                raw_meta: Any = r.metadata if hasattr(r, "metadata") else {}
                meta: dict[str, Any] = (
                    cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
                )
                # Freshness check
                if not self._is_fresh(meta):
                    continue
                # Resolution check
                if meta.get("resolved"):
                    continue
                r_tool = str(meta.get("tool_name", ""))
                r_file = str(meta.get("file_path", ""))
                if r_file and self._is_resolved(r_tool, r_file):
                    continue
                filtered.append(r.text)
                if len(filtered) >= top_k:
                    break
            return filtered
        except Exception as exc:
            logger.debug("Failed to recall tool failures: %s", exc)
            return []

    def recall_for_file(
        self,
        file_path: str,
        top_k: int = 3,
    ) -> list[str]:
        """Retrieve past failures related to a specific file (fresh + unresolved only)."""
        if not self._available:
            return []
        try:
            results = self._store.search_similar(
                query=f"eval failure on file {file_path}",
                namespace=_EVAL_NAMESPACE,
                top_k=top_k * 2,
                memory_types=["eval_failure"],
                threshold=_RECALL_THRESHOLD,
            )
            filtered: list[str] = []
            for r in results:
                if r.score < _RECALL_THRESHOLD:
                    continue
                raw_meta: Any = r.metadata if hasattr(r, "metadata") else {}
                meta: dict[str, Any] = (
                    cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
                )
                if not self._is_fresh(meta):
                    continue
                r_tool = str(meta.get("tool_name", ""))
                r_file = str(meta.get("file_path", ""))
                if r_file and self._is_resolved(r_tool, r_file):
                    continue
                filtered.append(r.text)
                if len(filtered) >= top_k:
                    break
            return filtered
        except Exception as exc:
            logger.debug("Failed to recall file failures: %s", exc)
            return []

    def recall_for_context(
        self,
        tool_names: list[str] | None = None,
        file_paths: list[str] | None = None,
        top_k: int = 3,
    ) -> list[str]:
        """Context-aware recall (#1): only retrieve failures relevant to
        the tools and files about to be used.

        If neither tool_names nor file_paths is provided, returns nothing
        (no more blind "recent failures" injection).
        """
        if not self._available:
            return []

        warnings: list[str] = []
        seen: set[str] = set()

        # Recall per tool+file
        for tool in tool_names or []:
            for fp in file_paths or [""]:
                for w in self.recall_for_tool(tool, file_path=fp, top_k=2):
                    if w not in seen:
                        seen.add(w)
                        warnings.append(w)

        # Recall per file (cross-tool)
        for fp in file_paths or []:
            if fp:
                for w in self.recall_for_file(fp, top_k=2):
                    if w not in seen:
                        seen.add(w)
                        warnings.append(w)

        return warnings[:top_k]

    def format_warnings(self, warnings: list[str], max_chars: int = 500) -> str:
        """Format recall results as a context injection string."""
        if not warnings:
            return ""
        header = "⚠ Past eval failures relevant to this task (avoid repeating):\n"
        body = ""
        for w in warnings:
            line = f"  - {w}\n"
            if len(header) + len(body) + len(line) > max_chars:
                break
            body += line
        return header + body if body else ""

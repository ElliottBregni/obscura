"""obscura.cli.vector_memory_bridge — Vector memory integration for the CLI REPL.

Provides helpers for:
- Session-start memory retrieval
- Pre-message context injection (search before each user turn)
- Post-message auto-save (store conversation turns)
- Formatting vector results into system prompt sections
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser
    from obscura.vector_memory.vector_memory import VectorMemoryStore

_logger = logging.getLogger(__name__)

# Namespace used for all CLI auto-saved memories
CLI_NAMESPACE = "cli:conversation"


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def is_vector_memory_enabled() -> bool:
    """Check if vector memory is enabled via env var.

    Defaults to True. Set OBSCURA_VECTOR_MEMORY=off to disable.
    """
    val = os.environ.get("OBSCURA_VECTOR_MEMORY", "on").strip().lower()
    return val not in ("off", "false", "0", "no")


# ---------------------------------------------------------------------------
# Memory injection mode + project scope
# ---------------------------------------------------------------------------
#
# Why we don't search every turn:
#   The original RAG pattern searched on every user message, top-k=3,
#   threshold=0.1, namespace=None — pulling top hits across every
#   namespace. In practice that meant the ~200-entry ``cli:conversation``
#   noise dominated, and barely-relevant snippets from unrelated projects
#   leaked in (browser sessions, other repos). For continuation turns
#   ("fix the typo") the conversation history already covers what's
#   needed; the search is pure budget burn.
#
# Mode "first" (default): inject vector memory only on the first user
#   turn of a session — the cold-start prime. Subsequent turns rely on
#   ``ctx.message_history``.
# Mode "every": legacy behaviour, search on every turn.
# Mode "off": never inject.

InjectionMode = Literal["first", "every", "off"]


def get_memory_injection_mode() -> InjectionMode:
    """Read ``OBSCURA_MEMORY_INJECTION_MODE`` (first | every | off)."""
    val = os.environ.get("OBSCURA_MEMORY_INJECTION_MODE", "first").strip().lower()
    if val in ("first", "every", "off"):
        return val  # type: ignore[return-value]
    _logger.warning(
        "Unknown OBSCURA_MEMORY_INJECTION_MODE=%r; defaulting to 'first'", val
    )
    return "first"


def is_project_scope_enabled() -> bool:
    """Read ``OBSCURA_MEMORY_PROJECT_SCOPE`` (defaults to on).

    When on, ``auto_save_turn`` tags each saved memory with a stable
    ``project_key`` (git toplevel → cwd hash) and the search functions
    drop entries whose ``project_key`` doesn't match the current one.
    Memories saved before this knob existed have no ``project_key`` and
    are excluded under scope-on — that's intended; they're indistinct
    cross-project noise.
    """
    val = os.environ.get("OBSCURA_MEMORY_PROJECT_SCOPE", "on").strip().lower()
    return val not in ("off", "false", "0", "no")


def derive_project_key(start_dir: str | os.PathLike[str] | None = None) -> str:
    """Return a short stable identifier for the current project.

    Prefers ``git rev-parse --show-toplevel`` (so subdirectory cwds in
    the same repo collapse to one key); falls back to the absolute
    ``start_dir``. The result is a 12-char SHA-1 prefix — short enough
    for log lines, stable across sessions in the same project, and
    distinct between projects.
    """
    base = Path(start_dir or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if result.returncode == 0:
            top = result.stdout.strip()
            if top:
                base = Path(top).resolve()
    except Exception:
        # No git, no problem — fall back to cwd. Don't let project-key
        # derivation block the turn.
        pass
    return hashlib.sha1(str(base).encode("utf-8")).hexdigest()[:12]


def _filter_results_by_project(
    results: list[Any],
    project_key: str,
) -> list[Any]:
    """Drop results whose metadata ``project_key`` doesn't match.

    Pre-scope-tag entries (no ``project_key`` in metadata) are dropped
    so cross-project noise stops bleeding in. Once project-scoping is
    on for a few sessions, the unscoped entries decay out via TTL.
    """
    kept: list[Any] = []
    for r in results:
        meta = getattr(r, "metadata", None) or {}
        if meta.get("project_key") == project_key:
            kept.append(r)
    return kept


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def init_vector_store(user: AuthenticatedUser) -> VectorMemoryStore | None:
    """Initialize a VectorMemoryStore for the CLI session.

    Returns None if vector memory is disabled or initialization fails.
    """
    if not is_vector_memory_enabled():
        return None
    try:
        from obscura.vector_memory import VectorMemoryStore

        return VectorMemoryStore.for_user(user)
    except Exception as e:
        _logger.warning(f"Could not initialize vector memory: {e}")
        return None


# ---------------------------------------------------------------------------
# Session-start context retrieval
# ---------------------------------------------------------------------------


def load_startup_memories(
    store: VectorMemoryStore,
    session_id: str,
    top_k: int = 3,
) -> str:
    """Search vector memory for recent/relevant context at session start.

    Uses a broad query to find recent important memories.
    Returns formatted string for injection into system prompt, or "".
    """
    try:
        results = store.search_reranked(
            query="recent conversation context and important information",
            namespace=CLI_NAMESPACE,
            top_k=top_k,
            recency_weight=0.6,
        )
        if not results:
            return ""
        return _format_memories_section(
            results,
            header="## Recalled Memories (from previous sessions)",
        )
    except Exception as e:
        _logger.warning(f"Could not load startup memories: {e}")
        return ""


# ---------------------------------------------------------------------------
# Pre-message search (RAG-style context injection)
# ---------------------------------------------------------------------------


def search_relevant_context(
    store: VectorMemoryStore,
    query: str,
    top_k: int = 3,
    threshold: float = 0.1,
    project_key: str | None = None,
) -> str:
    """Search vector memory for context relevant to the user's message.

    Returns a formatted context block to prepend to the user message,
    or "" if no relevant memories found.

    When *project_key* is provided, results are post-filtered to entries
    tagged with the matching ``project_key`` in metadata. Pre-scope
    entries (saved before this knob existed) are dropped — that's
    intentional, they're indistinct cross-project noise. We
    over-fetch (``top_k * 4``) so post-filter still has a fighting
    chance of returning ``top_k`` matches.
    """
    fetch_k = top_k * 4 if project_key is not None else top_k
    try:
        results = store.search_reranked(
            query=query,
            namespace=None,
            top_k=fetch_k,
            recency_weight=0.2,
        )
        results = [r for r in results if r.score > threshold]
        if project_key is not None:
            results = _filter_results_by_project(results, project_key)
        results = results[:top_k]
        if not results:
            return ""
        return _format_memories_section(
            results,
            header="[Relevant context from memory]",
        )
    except Exception as e:
        _logger.debug(f"Vector memory search failed: {e}")
        return ""


def search_with_router(
    router: Any,
    text: str,
    project_key: str | None = None,
) -> str:
    """Query active memory channels via the :class:`ContextRouter`.

    Updates signals from the user text, then queries matched channels.
    Returns formatted context block, or ``""`` if no channels matched.

    *project_key* is currently advisory — the router queries channels
    individually and we don't post-filter its formatted output. Project
    scoping for the router would mean tagging channels themselves;
    that's a follow-up.
    """
    try:
        router.update_signals_from_text(text)
        return router.query_active_channels(query=text)
    except Exception as e:
        _logger.debug(f"Channel router query failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Post-message auto-save
# ---------------------------------------------------------------------------


def auto_save_turn(
    store: VectorMemoryStore,
    session_id: str,
    user_text: str,
    assistant_text: str,
    turn_number: int,
    classifier: Any | None = None,
    project_key: str | None = None,
) -> None:
    """Save a conversation turn to vector memory in a background thread.

    Saves a combined summary of the user message and assistant response.
    When a :class:`TurnClassifier` is provided, the turn is also saved to
    matched channel namespaces.
    Runs in a daemon thread so it does not block the REPL.

    *project_key* is stored in metadata so subsequent searches can scope
    to entries from the current project. Falls back to deriving from
    cwd when not provided so callers that haven't yet been updated still
    get a tag.
    """

    def _save() -> None:
        try:
            # Skip persisting transport/debug noise from MCP server logs.
            if _is_mcp_noise_turn(user_text, assistant_text):
                return

            timestamp = datetime.now(UTC).isoformat()
            base_key = f"turn_{session_id}_{turn_number}_{timestamp}"

            user_snippet = user_text[:500]
            assistant_snippet = assistant_text[:1000]

            combined = f"User: {user_snippet}\nAssistant: {assistant_snippet}"

            meta = {
                "session_id": session_id,
                "turn": turn_number,
                "timestamp": timestamp,
                "user_message_preview": user_text[:100],
                "project_key": project_key or derive_project_key(),
            }

            # Determine namespaces to save to
            namespaces = [CLI_NAMESPACE]
            if classifier is not None:
                try:
                    namespaces = classifier.classify(user_text, assistant_text)
                except Exception:
                    _logger.debug("Turn classification failed, using default namespace")

            for ns in namespaces:
                key = f"{base_key}_{ns}" if ns != CLI_NAMESPACE else base_key
                store.set(
                    key=key,
                    text=combined,
                    metadata=meta,
                    namespace=ns,
                    memory_type="episode",
                    ttl=timedelta(days=30),
                )
        except Exception as e:
            _logger.debug(f"Auto-save to vector memory failed: {e}")

        # Auto-learn profile facts from user messages (best-effort, silent).
        try:
            from obscura.auth.context import current_user
            from obscura.profile.learner import ProfileLearner
            from obscura.profile.store import ProfileStore

            user = current_user()
            profile_store = ProfileStore.for_user(user, vector_store=store)
            learner = ProfileLearner(profile_store)
            new_facts = learner.process_turn(user_text)
            if new_facts:
                _logger.debug(
                    "Auto-learned %d profile facts from turn %d",
                    len(new_facts),
                    turn_number,
                )
        except Exception:
            _logger.debug("Profile auto-learn skipped", exc_info=True)

    thread = threading.Thread(target=_save, daemon=True)
    thread.start()


def run_startup_maintenance(store: VectorMemoryStore) -> None:
    """Run decay maintenance in a background daemon thread.

    Called at session start when ``maintenance_on_startup`` is enabled.
    Non-blocking — the REPL is not held up.
    """
    if not getattr(store, "decay_config", None):
        return
    if not store.decay_config.maintenance_on_startup:
        return

    def _run() -> None:
        try:
            report = store.run_maintenance()
            if report.expired_purged or report.episodes_consolidated:
                _logger.info(
                    "Startup maintenance: purged=%d, consolidated=%d, summaries=%d (%.0fms)",
                    report.expired_purged,
                    report.episodes_consolidated,
                    report.summaries_created,
                    report.duration_ms,
                )
        except Exception:
            _logger.debug("Startup maintenance failed", exc_info=True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def clear_mcp_noise_memories(store: VectorMemoryStore) -> int:
    """Delete only MCP-log-like memories from the CLI namespace."""
    removed = 0
    try:
        keys = store.list_keys(namespace=CLI_NAMESPACE)
    except Exception:
        return 0

    for key in keys:
        try:
            entry = store.get(key)
            if entry is None:
                continue
            if _is_mcp_noise_text(entry.text) and store.delete(key):
                removed += 1
        except Exception:
            continue
    return removed


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_memories_section(
    entries: list[Any],
    header: str = "## Relevant Memories",
    max_text_len: int = 300,
) -> str:
    """Format vector search results into a readable context section."""
    lines = [header, ""]
    for i, entry in enumerate(entries, 1):
        text = entry.text
        if len(text) > max_text_len:
            text = text[:max_text_len] + "..."
        score_str = f"{entry.score:.2f}"
        lines.append(f"{i}. (score: {score_str}) {text}")
        lines.append("")
    return "\n".join(lines)


def _is_mcp_noise_turn(user_text: str, assistant_text: str) -> bool:
    combined = f"{user_text}\n{assistant_text}"
    return _is_mcp_noise_text(combined)


def _is_mcp_noise_text(text: str) -> bool:
    s = text.lower()
    markers = (
        "mcp server",
        "/mcp ",
        "mcp:",
        "jsonrpc",
        "tool_use_start",
        "tool_use_delta",
        "tool_result",
        "invalid_request_body",
        "stdio transport",
        "anthropic.tools.beta.messages",
    )
    hit_count = sum(1 for marker in markers if marker in s)
    return hit_count >= 2

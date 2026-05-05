"""obscura.core.supervisor.vault_hook — Lazy vault sync at lifecycle boundaries.

Registers two hooks:
  - **PRE_BUILD_CONTEXT** — Ingest user-zone changes (if any) before the
    agent sees context.  Only does work when files have actually changed
    since last sync (hash comparison).  Also injects ``always_inject``
    files and a compact vault index into the prompt.
  - **PRE_FINALIZE** — Export Obscura state (goals, tasks, arbiter verdicts)
    to the agent zone so the vault stays current between sessions.

No file watcher, no background thread.  Sync happens at natural lifecycle
boundaries: once at session start, once at session end.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from obscura.core.supervisor.types import SupervisorHookPoint

if TYPE_CHECKING:
    from obscura.core.supervisor.session_hooks import SessionHookManager

logger = logging.getLogger(__name__)


def register_vault_hooks(hooks: SessionHookManager) -> None:
    """Register vault sync hooks on a supervisor SessionHookManager."""
    hooks.register(
        SupervisorHookPoint.PRE_BUILD_CONTEXT,
        "before",
        "vault:ingest_and_inject",
        _ingest_and_inject,
        persist=False,
    )
    hooks.register(
        SupervisorHookPoint.PRE_FINALIZE,
        "before",
        "vault:export",
        _export_on_finalize,
        persist=False,
    )
    logger.info("Vault hooks registered (ingest on build, export on finalize)")


async def _ingest_and_inject(context: dict[str, Any]) -> None:
    """PRE_BUILD_CONTEXT: ingest changed user-zone files and inject vault context."""
    try:
        from obscura.kairos.vault_sync import VaultSync

        vs = VaultSync()
        if not vs.vault_dir.is_dir():
            return

        # Bootstrap zones if missing (idempotent).
        vs.bootstrap()

        # Detect changes in user zone since last sync.
        changes = vs.detect_changes("user")
        changed_files = changes.added + changes.modified

        if changed_files:
            for meta in changed_files:
                try:
                    vs._ingest_file(meta)  # pyright: ignore[reportPrivateUsage]
                except Exception:
                    logger.debug("Vault ingest failed for %s", meta.path, exc_info=True)

            # Update hashes so we don't re-ingest next run.
            for meta in vs.scan("user"):
                vs._prev_hashes[str(meta.path)] = meta.hash  # pyright: ignore[reportPrivateUsage]
            vs._save_hashes()  # pyright: ignore[reportPrivateUsage]

            logger.info("Vault ingest: %d file(s) synced", len(changed_files))

        # Inject vault context into the prompt.
        # Use active goal text as query for relevance ranking.
        goal_ctx: str = context.get("_goal_context", "")
        vault_context = _build_vault_context(vs, query=goal_ctx or None)
        if vault_context:
            context["_vault_context"] = vault_context

    except Exception:
        logger.debug("Vault PRE_BUILD_CONTEXT hook failed", exc_info=True)


async def _export_on_finalize(context: dict[str, Any]) -> None:
    """PRE_FINALIZE: export Obscura state to the agent zone."""
    try:
        from obscura.kairos.vault_sync import VaultSync

        vs = VaultSync()
        if not vs.vault_dir.is_dir():
            return

        exported = vs._export_all()  # pyright: ignore[reportPrivateUsage]

        # Update hashes for all zones.
        for meta in vs.scan():
            vs._prev_hashes[str(meta.path)] = meta.hash  # pyright: ignore[reportPrivateUsage]
        vs._save_hashes()  # pyright: ignore[reportPrivateUsage]

        logger.info("Vault export: %d file(s) written", exported)
    except Exception:
        logger.debug("Vault PRE_FINALIZE export failed", exc_info=True)


def _vault_relevance_score(meta: Any, query_words: set[str]) -> int:
    """Score a vault file by keyword overlap with query_words.

    Checks title, tags, type, and first 500 chars of body.
    Returns number of matching keywords (higher = more relevant).
    """
    if not query_words:
        return 0
    candidates: list[str] = []
    fm = meta.frontmatter
    if fm.get("title"):
        candidates.append(str(fm["title"]).lower())
    if fm.get("tags"):
        tags = fm["tags"]
        if isinstance(tags, list):
            candidates.extend(str(t).lower() for t in cast(list[Any], tags))
        else:
            candidates.append(str(tags).lower())
    if fm.get("type"):
        candidates.append(str(fm["type"]).lower())
    if meta.body:
        candidates.append(meta.body[:500].lower())
    combined = " ".join(candidates)
    return sum(1 for w in query_words if w in combined)


def _build_vault_context(vs: Any, *, query: str | None = None) -> str:
    """Build the vault context section for prompt injection.

    Three parts:
    1. Full content of ``always_inject: true`` files (profile, conventions)
    2. Semantically ranked vault index -- top-5 relevant files shown with a
       one-line snippet, remaining files listed by name only
    3. Instruction telling the agent to ``read_text_file`` for details

    When *query* is provided (e.g. the active goal summary), files are ranked
    by keyword overlap so the most relevant notes surface first.
    """
    parts: list[str] = ["## Vault"]

    # 1. Always-inject files (full content, small and curated).
    always_files = [
        m for m in vs.scan("user") if m.frontmatter.get("always_inject") is True
    ]
    for meta in always_files:
        if meta.body:
            parts.append(f"\n### {meta.frontmatter.get('title', meta.path.stem)}")
            parts.append(meta.body)

    # 2. Vault index -- ranked by relevance when a query is available.
    all_files = vs.scan()
    if all_files:
        # Skip always_inject files from index (already shown in full).
        always_paths = {str(m.path) for m in always_files}
        index_files = [m for m in all_files if str(m.path) not in always_paths]

        if index_files:
            # Build query word set for relevance scoring.
            stop = {
                "the",
                "a",
                "an",
                "and",
                "or",
                "of",
                "to",
                "in",
                "is",
                "for",
                "with",
                "this",
                "that",
                "are",
                "on",
                "at",
                "be",
            }
            query_words: set[str] = set()
            if query:
                query_words = {
                    w for w in query.lower().split() if len(w) > 2 and w not in stop
                }

            # Sort by relevance score descending (stable -- preserves order on ties).
            scored = sorted(
                index_files,
                key=lambda m: _vault_relevance_score(m, query_words),
                reverse=True,
            )

            _TOP_N = 5  # Show snippet for top-N relevant files.
            parts.append("\n### Vault Index")
            if query_words:
                parts.append("Relevant files (ranked by topic match):")
            else:
                parts.append("Files available via `read_text_file`:")

            for i, meta in enumerate(scored):
                rel = meta.path.relative_to(vs.vault_dir)
                file_type = meta.frontmatter.get("type", "")
                title = meta.frontmatter.get("title", "")
                label = f"  - `{rel}`"
                if file_type:
                    label += f" ({file_type})"
                if title:
                    label += f" — {title}"
                # Add a one-line body snippet for the top-N results.
                if i < _TOP_N and meta.body and query_words:
                    snippet = meta.body.strip().splitlines()[0][:120]
                    if snippet:
                        label += f"\n      _{snippet}_"
                parts.append(label)

    if len(parts) <= 1:
        return ""  # Nothing to inject.

    return "\n".join(parts)


__all__ = ["register_vault_hooks"]

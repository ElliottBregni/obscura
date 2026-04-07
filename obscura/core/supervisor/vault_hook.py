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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.core.supervisor.session_hooks import SessionHookManager

logger = logging.getLogger(__name__)


def register_vault_hooks(hooks: SessionHookManager) -> None:
    """Register vault sync hooks on a supervisor SessionHookManager."""
    from obscura.core.supervisor.types import SupervisorHookPoint

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
                    vs._ingest_file(meta)
                except Exception:
                    logger.debug("Vault ingest failed for %s", meta.path, exc_info=True)

            # Update hashes so we don't re-ingest next run.
            for meta in vs.scan("user"):
                vs._prev_hashes[str(meta.path)] = meta.hash
            vs._save_hashes()

            logger.info("Vault ingest: %d file(s) synced", len(changed_files))

        # Inject vault context into the prompt.
        vault_context = _build_vault_context(vs)
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

        exported = vs._export_all()

        # Update hashes for all zones.
        for meta in vs.scan():
            vs._prev_hashes[str(meta.path)] = meta.hash
        vs._save_hashes()

        logger.info("Vault export: %d file(s) written", exported)
    except Exception:
        logger.debug("Vault PRE_FINALIZE export failed", exc_info=True)


def _build_vault_context(vs: Any) -> str:
    """Build the vault context section for prompt injection.

    Three parts:
    1. Full content of ``always_inject: true`` files (profile, conventions)
    2. Compact index of all vault files so the agent knows what's available
    3. Instruction telling the agent to ``read_text_file`` for details
    """
    parts: list[str] = ["## Vault"]

    # 1. Always-inject files (full content, small and curated).
    always_files = [
        m for m in vs.scan("user")
        if m.frontmatter.get("always_inject") is True
    ]
    for meta in always_files:
        if meta.body:
            parts.append(f"\n### {meta.frontmatter.get('title', meta.path.stem)}")
            parts.append(meta.body)

    # 2. Vault index (compact — just filenames and types).
    all_files = vs.scan()
    if all_files:
        # Skip always_inject files from index (already shown in full).
        always_paths = {str(m.path) for m in always_files}
        index_files = [m for m in all_files if str(m.path) not in always_paths]

        if index_files:
            parts.append("\n### Vault Index")
            parts.append("Files available via `read_text_file`:")
            for meta in index_files:
                rel = meta.path.relative_to(vs.vault_dir)
                file_type = meta.frontmatter.get("type", "")
                label = f"  - `{rel}`"
                if file_type:
                    label += f" ({file_type})"
                title = meta.frontmatter.get("title", "")
                if title:
                    label += f" — {title}"
                parts.append(label)

    if len(parts) <= 1:
        return ""  # Nothing to inject.

    return "\n".join(parts)


__all__ = ["register_vault_hooks"]

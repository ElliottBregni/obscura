"""Filesystem mutation tools (write/append/edit/copy/move/mkdir/remove/diff)."""

from __future__ import annotations

import difflib
import fcntl
import json
import shutil

from obscura.core.tools import tool
from obscura.tools.system._policy import Policy
from obscura.tools.system.diff_utils import compute_unified_diff
from obscura.tools.system.file_state import check_staleness
import logging

logger = logging.getLogger(__name__)


class FsWrite:
    """Filesystem-mutation tool namespace."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_quotes(text: str) -> str:
        """Normalize curly/smart quotes to straight ASCII quotes."""
        replacements = {
            "‘": "'",
            "’": "'",  # Single curly quotes
            "“": '"',
            "”": '"',  # Double curly quotes
            "′": "'",
            "″": '"',  # Prime marks
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    @staticmethod
    def merge_lines(old: list[str], new: list[str]) -> tuple[list[str], bool]:
        """Merge new content into old using line-level difflib opcodes.

        Returns (merged_lines, had_conflict). When both sides changed the same
        lines the conflicting region is wrapped in standard conflict markers so
        the caller can detect and surface the situation.
        """
        import difflib

        matcher = difflib.SequenceMatcher(None, old, new)
        result: list[str] = []
        had_conflict = False
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                result.extend(old[i1:i2])
            elif tag == "replace":
                # Both sides differ — emit conflict markers.
                result.append("<<<<<<< agent\n")
                result.extend(new[j1:j2])
                result.append("=======\n")
                result.extend(old[i1:i2])
                result.append(">>>>>>> previous\n")
                had_conflict = True
            elif tag == "insert":
                result.extend(new[j1:j2])
            elif tag == "delete":
                pass  # old content removed by new version
        return result, had_conflict

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "write_text_file",
        (
            "Write UTF-8 text to a file (overwrites by default). "
            "For existing files, rejects stale writes if the file was modified "
            "externally since the last read. Returns a structured diff."
        ),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "create_dirs": {"type": "boolean"},
            },
            "required": ["path", "text"],
        },
    )
    async def write_text_file(
        path: str,
        text: str,
        overwrite: bool = True,
        create_dirs: bool = True,
    ) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target
        ):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not Policy.is_vault_write_allowed(target):
            return Policy.json_error(
                "vault_zone_readonly",
                path=str(target),
                detail="vault/user and vault/shared are read-only; write to vault/agent instead",
            )
        if target.exists() and target.is_dir():
            return Policy.json_error("path_is_directory", path=str(target))
        if target.exists() and not overwrite:
            return Policy.json_error("file_exists", path=str(target))

        is_new = not target.exists()
        original = ""

        if not is_new:
            # Staleness check for existing files.
            staleness_err = check_staleness(target)
            if staleness_err is not None:
                return Policy.json_error(
                    "stale_file", path=str(target), detail=staleness_err
                )
            original = target.read_text(encoding="utf-8")
            # Preserve original line endings if the file uses CRLF.
            if "\r\n" in original and "\r\n" not in text:
                text = text.replace("\n", "\r\n")

        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

        # Generate diff.
        diff = compute_unified_diff(original, text, str(target))

        return json.dumps(
            {
                "ok": True,
                "path": str(target),
                "bytes_written": len(text.encode("utf-8")),
                "is_new": is_new,
                "diff": diff,
            },
        )

    @staticmethod
    @tool(
        "append_text_file",
        "Append UTF-8 text to a file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string"},
                "create_dirs": {"type": "boolean"},
            },
            "required": ["path", "text"],
        },
    )
    async def append_text_file(path: str, text: str, create_dirs: bool = True) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target
        ):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not Policy.is_vault_write_allowed(target):
            return Policy.json_error(
                "vault_zone_readonly",
                path=str(target),
                detail="vault/user and vault/shared are read-only; write to vault/agent instead",
            )
        if target.exists() and target.is_dir():
            return Policy.json_error("path_is_directory", path=str(target))

        is_new = not target.exists()
        original = ""
        if not is_new:
            try:
                original = target.read_text(encoding="utf-8")
            except OSError:
                logger.debug("suppressed exception in append_text_file", exc_info=True)
                original = ""

        try:
            if create_dirs:
                target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            logger.debug("suppressed exception in append_text_file", exc_info=True)
            return Policy.json_error("append_failed", path=str(target), detail=str(exc))

        after = original + text
        diff = compute_unified_diff(original, after, str(target))
        total_lines = after.count("\n") + (
            1 if after and not after.endswith("\n") else 0
        )
        return json.dumps(
            {
                "ok": True,
                "path": str(target),
                "bytes_appended": len(text.encode("utf-8")),
                "is_new": is_new,
                "total_lines": total_lines,
                "diff": diff,
            },
        )

    @staticmethod
    @tool(
        "make_directory",
        "Create a directory path.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "parents": {"type": "boolean"},
                "exist_ok": {"type": "boolean"},
            },
            "required": ["path"],
        },
    )
    async def make_directory(
        path: str,
        parents: bool = True,
        exist_ok: bool = True,
    ) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target
        ):
            return Policy.json_error("path_not_allowed", path=str(target))
        try:
            target.mkdir(parents=parents, exist_ok=exist_ok)
        except OSError as exc:
            logger.debug("suppressed exception in make_directory", exc_info=True)
            return Policy.json_error("mkdir_failed", path=str(target), detail=str(exc))
        return json.dumps({"ok": True, "path": str(target)})

    @staticmethod
    @tool(
        "remove_path",
        "Remove a file or directory recursively when requested.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean"},
                "missing_ok": {"type": "boolean"},
            },
            "required": ["path"],
        },
    )
    async def remove_path(
        path: str,
        recursive: bool = False,
        missing_ok: bool = True,
    ) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target
        ):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not Policy.is_vault_write_allowed(target):
            return Policy.json_error(
                "vault_zone_readonly",
                path=str(target),
                detail="vault/user and vault/shared are read-only; write to vault/agent instead",
            )
        if not target.exists():
            if missing_ok:
                return json.dumps({"ok": True, "path": str(target), "removed": False})
            return Policy.json_error("path_not_found", path=str(target))

        if target.is_dir():
            if not recursive:
                return Policy.json_error(
                    "directory_requires_recursive_true", path=str(target)
                )
            shutil.rmtree(target)
            return json.dumps({"ok": True, "path": str(target), "removed": True})

        target.unlink(missing_ok=missing_ok)
        return json.dumps({"ok": True, "path": str(target), "removed": True})

    @staticmethod
    @tool(
        "edit_text_file",
        (
            "Perform a surgical find-and-replace edit in a file. "
            "Replaces the first (or all) occurrence(s) of old_text with new_text. "
            "The file must have been read first — rejects stale edits if the file "
            "was modified externally since the last read."
        ),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {
                    "type": "string",
                    "description": "Text to find (exact match).",
                },
                "new_text": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false).",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    )
    async def edit_text_file(
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target
        ):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not Policy.is_vault_write_allowed(target):
            return Policy.json_error(
                "vault_zone_readonly",
                path=str(target),
                detail="vault/user and vault/shared are read-only; write to vault/agent instead",
            )
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))
        if not target.is_file():
            return Policy.json_error("not_a_file", path=str(target))

        # Acquire an exclusive advisory lock around the read-modify-write
        # cycle to prevent TOCTOU races with other agents/processes.
        with target.open("r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                # Staleness check — must happen inside the lock so no
                # concurrent writer can slip in between check and read.
                staleness_err = check_staleness(target)
                if staleness_err is not None:
                    return Policy.json_error(
                        "stale_file", path=str(target), detail=staleness_err
                    )

                content = fh.read()

                # Try exact match first, then quote-normalized fallback.
                actual_old = old_text
                if old_text not in content:
                    normalized_old = FsWrite.normalize_quotes(old_text)
                    normalized_content = FsWrite.normalize_quotes(content)
                    if normalized_old in normalized_content:
                        # Find the actual substring in original content by position.
                        pos = normalized_content.index(normalized_old)
                        actual_old = content[pos : pos + len(old_text)]
                        if actual_old not in content:
                            return Policy.json_error(
                                "text_not_found",
                                path=str(target),
                                old_text=old_text[:200],
                            )
                    else:
                        return Policy.json_error(
                            "text_not_found",
                            path=str(target),
                            old_text=old_text[:200],
                        )

                if replace_all:
                    new_content = content.replace(actual_old, new_text)
                    count = content.count(actual_old)
                else:
                    new_content = content.replace(actual_old, new_text, 1)
                    count = 1

                # Write back while still holding the lock.
                fh.seek(0)
                fh.write(new_content)
                fh.truncate()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

        # Generate structured diff for the response.
        diff = compute_unified_diff(content, new_content, str(target))

        return json.dumps(
            {
                "ok": True,
                "path": str(target),
                "replacements": count,
                "bytes_written": len(new_content.encode("utf-8")),
                "diff": diff,
            },
        )

    @staticmethod
    @tool(
        "copy_path",
        "Copy a file or directory to a new location.",
        {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["src", "dst"],
        },
    )
    async def copy_path(src: str, dst: str, overwrite: bool = False) -> str:
        src_path = Policy.resolve_path(src)
        dst_path = Policy.resolve_path(dst)
        if not Policy.unsafe_full_access_enabled():
            if not Policy.is_path_allowed(src_path):
                return Policy.json_error("path_not_allowed", path=str(src_path))
            if not Policy.is_path_allowed(dst_path):
                return Policy.json_error("path_not_allowed", path=str(dst_path))
        if not src_path.exists():
            return Policy.json_error("path_not_found", path=str(src_path))
        if dst_path.exists() and not overwrite:
            return Policy.json_error("destination_exists", path=str(dst_path))

        if src_path.is_dir():
            if dst_path.exists():
                shutil.rmtree(dst_path)
            shutil.copytree(src_path, dst_path)
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)

        return json.dumps({"ok": True, "src": str(src_path), "dst": str(dst_path)})

    @staticmethod
    @tool(
        "move_path",
        "Move or rename a file or directory.",
        {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["src", "dst"],
        },
    )
    async def move_path(src: str, dst: str, overwrite: bool = False) -> str:
        src_path = Policy.resolve_path(src)
        dst_path = Policy.resolve_path(dst)
        if not Policy.unsafe_full_access_enabled():
            if not Policy.is_path_allowed(src_path):
                return Policy.json_error("path_not_allowed", path=str(src_path))
            if not Policy.is_path_allowed(dst_path):
                return Policy.json_error("path_not_allowed", path=str(dst_path))
        if not src_path.exists():
            return Policy.json_error("path_not_found", path=str(src_path))
        if dst_path.exists() and not overwrite:
            return Policy.json_error("destination_exists", path=str(dst_path))

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
        return json.dumps({"ok": True, "src": str(src_path), "dst": str(dst_path)})

    @staticmethod
    @tool(
        "diff_files",
        "Compare two files and return a unified diff.",
        {
            "type": "object",
            "properties": {
                "file_a": {"type": "string"},
                "file_b": {"type": "string"},
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context (default 3).",
                },
            },
            "required": ["file_a", "file_b"],
        },
    )
    async def diff_files(file_a: str, file_b: str, context_lines: int = 3) -> str:
        path_a = Policy.resolve_path(file_a)
        path_b = Policy.resolve_path(file_b)
        if not Policy.unsafe_full_access_enabled():
            if not Policy.is_path_allowed(path_a):
                return Policy.json_error("path_not_allowed", path=str(path_a))
            if not Policy.is_path_allowed(path_b):
                return Policy.json_error("path_not_allowed", path=str(path_b))
        if not path_a.exists():
            return Policy.json_error("path_not_found", path=str(path_a))
        if not path_b.exists():
            return Policy.json_error("path_not_found", path=str(path_b))

        try:
            lines_a = path_a.read_text(encoding="utf-8", errors="replace").splitlines(
                keepends=True,
            )
            lines_b = path_b.read_text(encoding="utf-8", errors="replace").splitlines(
                keepends=True,
            )
        except OSError as exc:
            logger.debug("suppressed exception in diff_files", exc_info=True)
            return Policy.json_error("read_failed", detail=str(exc))

        try:
            context_lines = int(context_lines)
        except (TypeError, ValueError):
            logger.debug("suppressed exception in diff_files", exc_info=True)
            context_lines = 3
        ctx = max(0, min(context_lines, 20))
        diff = list(
            difflib.unified_diff(
                lines_a,
                lines_b,
                fromfile=str(path_a),
                tofile=str(path_b),
                n=ctx,
            ),
        )
        diff_text = "".join(diff)
        return json.dumps(
            {
                "ok": True,
                "file_a": str(path_a),
                "file_b": str(path_b),
                "identical": len(diff) == 0,
                "diff": diff_text[:100_000],
            },
        )

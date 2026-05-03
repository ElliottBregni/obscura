"""Grep / content search tools for the system toolset."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import shutil
from pathlib import Path
from typing import Any

from obscura.core.tools import tool
from obscura.tools.system._policy import Policy
import logging

logger = logging.getLogger(__name__)


class Grep:
    """Grep / content-search tool namespace."""

    # ------------------------------------------------------------------
    # Helpers (also exposed as classmethods so other modules can call them)
    # ------------------------------------------------------------------

    @staticmethod
    async def grep_via_ripgrep(
        *,
        rg_path: str,
        pattern: str,
        target: Path,
        include: str,
        glob_pattern: str,
        file_type: str,
        output_mode: str,
        context: int,
        before_context: int,
        after_context: int,
        case_sensitive: bool,
        multiline: bool,
        head_limit: int,
        offset: int,
    ) -> str:
        """Execute grep via ripgrep subprocess and parse results."""
        cmd: list[str] = [rg_path, "--no-heading", "--with-filename", "--line-number"]

        if not case_sensitive:
            cmd.append("-i")
        if multiline:
            cmd.extend(["-U", "--multiline-dotall"])
        if context > 0:
            cmd.extend(["-C", str(context)])
        else:
            if before_context > 0:
                cmd.extend(["-B", str(before_context)])
            if after_context > 0:
                cmd.extend(["-A", str(after_context)])

        # File filtering.
        if include:
            cmd.extend(["--glob", include])
        if glob_pattern:
            for g in glob_pattern.split():
                cmd.extend(["--glob", g])
        if file_type:
            cmd.extend(["--type", file_type])

        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")

        cmd.extend(["--", pattern, str(target)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=30.0,
            )
        except TimeoutError:
            logger.debug("suppressed exception in grep_via_ripgrep", exc_info=True)
            proc.kill()
            await proc.wait()
            return Policy.json_error("timeout", detail="ripgrep timed out after 30s")

        raw = stdout_bytes.decode("utf-8", errors="replace")
        lines = [ln for ln in raw.splitlines() if ln]

        # Apply offset/limit pagination.
        if offset > 0:
            lines = lines[offset:]
        if head_limit > 0:
            truncated = len(lines) > head_limit
            lines = lines[:head_limit]
        else:
            truncated = False

        if output_mode == "files_with_matches":
            # Sort by mtime (most recent first).
            def _mtime(fp: str) -> float:
                try:
                    return Path(fp).stat().st_mtime
                except OSError:
                    logger.debug("suppressed exception in _mtime", exc_info=True)
                    return 0.0

            files = sorted(lines, key=_mtime, reverse=True)
            return json.dumps(
                {
                    "ok": True,
                    "mode": "files_with_matches",
                    "pattern": pattern,
                    "path": str(target),
                    "count": len(files),
                    "truncated": truncated,
                    "files": files,
                },
            )

        if output_mode == "count":
            total_matches = 0
            count_entries: list[dict[str, object]] = []
            for ln in lines:
                if ":" in ln:
                    fp, cnt = ln.rsplit(":", 1)
                    try:
                        c = int(cnt.strip())
                    except ValueError:
                        logger.debug(
                            "suppressed exception in grep_via_ripgrep", exc_info=True
                        )
                        c = 0
                    count_entries.append({"file": fp, "count": c})
                    total_matches += c
            return json.dumps(
                {
                    "ok": True,
                    "mode": "count",
                    "pattern": pattern,
                    "path": str(target),
                    "num_files": len(count_entries),
                    "total_matches": total_matches,
                    "truncated": truncated,
                    "counts": count_entries,
                },
            )

        # Default: content mode.
        matches: list[dict[str, object]] = []
        for ln in lines:
            # Format: file:line:content  or  file-line-content (context)
            parts = ln.split(":", 2) if ":" in ln else [ln]
            if len(parts) >= 3:
                matches.append(
                    {"file": parts[0], "line": parts[1], "text": parts[2][:500]}
                )
            else:
                matches.append({"text": ln[:500]})

        return json.dumps(
            {
                "ok": True,
                "mode": "content",
                "pattern": pattern,
                "path": str(target),
                "count": len(matches),
                "truncated": truncated,
                "matches": matches,
            },
        )

    @staticmethod
    async def grep_via_python(
        *,
        pattern: str,
        target: Path,
        include: str,
        case_sensitive: bool,
        output_mode: str,
        head_limit: int,
        offset: int,
    ) -> str:
        """Fallback grep using Python re when ripgrep is unavailable."""
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            logger.debug("suppressed exception in grep_via_python", exc_info=True)
            return Policy.json_error("invalid_regex", pattern=pattern, detail=str(exc))

        _BINARY_EXTS = {
            ".pyc",
            ".pyo",
            ".so",
            ".dylib",
            ".bin",
            ".exe",
            ".o",
            ".a",
            ".class",
            ".jar",
            ".whl",
            ".gz",
            ".zip",
            ".tar",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".ico",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
        }

        limit = max(1, head_limit) if head_limit > 0 else 10_000
        matches: list[dict[str, object]] = []
        file_counts: dict[str, int] = {}
        matched_files: list[str] = []

        def _search_file(fp: Path) -> None:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                logger.debug("suppressed exception in _search_file", exc_info=True)
                return
            file_match_count = 0
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    file_match_count += 1
                    if output_mode == "content":
                        matches.append(
                            {
                                "file": str(fp),
                                "line": lineno,
                                "text": line.rstrip()[:500],
                            },
                        )
            if file_match_count > 0:
                file_counts[str(fp)] = file_match_count
                matched_files.append(str(fp))

        if target.is_file():
            _search_file(target)
        else:
            _RGLOB_CAP = 100_000

            def _do_rglob() -> list[Path]:
                out: list[Path] = []
                for fp in target.rglob("*"):
                    out.append(fp)
                    if len(out) >= _RGLOB_CAP:
                        break
                return sorted(out)

            try:
                rglob_paths = await asyncio.wait_for(
                    asyncio.to_thread(_do_rglob),
                    timeout=30.0,
                )
            except TimeoutError:
                logger.debug("suppressed exception in grep_via_python", exc_info=True)
                return Policy.json_error(
                    "timeout",
                    path=str(target),
                    detail="rglob timed out after 30s",
                )

            for fp in rglob_paths:
                if not fp.is_file():
                    continue
                if include and not fnmatch.fnmatch(fp.name, include):
                    continue
                if fp.suffix in _BINARY_EXTS:
                    continue
                _search_file(fp)

        if output_mode == "files_with_matches":
            results = matched_files[offset:]
            truncated = len(results) > limit
            results = results[:limit]
            return json.dumps(
                {
                    "ok": True,
                    "mode": "files_with_matches",
                    "pattern": pattern,
                    "path": str(target),
                    "count": len(results),
                    "truncated": truncated,
                    "files": results,
                },
            )

        if output_mode == "count":
            count_pairs: list[tuple[str, int]] = list(file_counts.items())
            count_pairs = count_pairs[offset:]
            truncated = len(count_pairs) > limit
            count_pairs = count_pairs[:limit]
            entries = [{"file": f, "count": c} for f, c in count_pairs]
            return json.dumps(
                {
                    "ok": True,
                    "mode": "count",
                    "pattern": pattern,
                    "path": str(target),
                    "num_files": len(entries),
                    "total_matches": sum(c for _, c in count_pairs),
                    "truncated": truncated,
                    "counts": entries,
                },
            )

        # Content mode.
        paginated = matches[offset:]
        truncated = len(paginated) > limit
        paginated = paginated[:limit]
        return json.dumps(
            {
                "ok": True,
                "mode": "content",
                "pattern": pattern,
                "path": str(target),
                "count": len(paginated),
                "truncated": truncated,
                "matches": paginated,
            },
        )

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "grep_files",
        (
            "Search file contents with regex. Supports multiple output modes: "
            "'content' shows matching lines, 'files_with_matches' shows file paths, "
            "'count' shows match counts. Uses ripgrep when available for speed."
        ),
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in.",
                },
                "include": {
                    "type": "string",
                    "description": "Glob filter for filenames (e.g. '*.py').",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern passed to rg --glob (e.g. '*.{ts,tsx}').",
                },
                "type": {
                    "type": "string",
                    "description": "File type filter for rg --type (e.g. 'py', 'js').",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output mode (default: 'content').",
                },
                "context": {
                    "type": "integer",
                    "description": "Context lines before and after each match (-C).",
                },
                "before_context": {
                    "type": "integer",
                    "description": "Lines before each match (-B).",
                },
                "after_context": {
                    "type": "integer",
                    "description": "Lines after each match (-A).",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive matching (default: true).",
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Enable multiline matching.",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Limit results (default: 250; 0=unlimited).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip first N results before applying head_limit.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Legacy alias for head_limit.",
                },
            },
            "required": ["pattern"],
        },
    )
    async def grep_files(
        pattern: str,
        path: str = ".",
        include: str = "",
        glob: str = "",
        output_mode: str = "content",
        context: int = 0,
        before_context: int = 0,
        after_context: int = 0,
        case_sensitive: bool = True,
        multiline: bool = False,
        head_limit: int = 250,
        offset: int = 0,
        max_results: int = 0,
        type: str = "",  # noqa: A002 — matches JSON schema property name
        **kwargs: Any,
    ) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target
        ):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))

        # Legacy compat: max_results overrides head_limit when provided.
        effective_limit = max_results if max_results > 0 else head_limit
        file_type = type or kwargs.get("type", "")

        # Try ripgrep first, fall back to Python implementation.
        rg_path = shutil.which("rg")
        if rg_path is not None:
            return await Grep.grep_via_ripgrep(
                rg_path=rg_path,
                pattern=pattern,
                target=target,
                include=include,
                glob_pattern=glob,
                file_type=str(file_type),
                output_mode=output_mode,
                context=context,
                before_context=before_context,
                after_context=after_context,
                case_sensitive=case_sensitive,
                multiline=multiline,
                head_limit=effective_limit,
                offset=offset,
            )

        return await Grep.grep_via_python(
            pattern=pattern,
            target=target,
            include=include,
            case_sensitive=case_sensitive,
            output_mode=output_mode,
            head_limit=effective_limit,
            offset=offset,
        )

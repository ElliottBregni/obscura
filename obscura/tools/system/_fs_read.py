"""Read-only filesystem tools (list, read, find, tree, info)."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import stat as stat_module
from pathlib import Path
from typing import Any

from obscura.core.tools import tool
from obscura.tools.system._policy import Policy


class FsRead:
    """Read-only filesystem tool namespace."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_page_range(pages: str, total: int) -> tuple[int, int]:
        """Parse a page range string like '1-5' or '3' into (start, end) 1-indexed."""
        pages = pages.strip()
        try:
            if "-" in pages:
                parts = pages.split("-", 1)
                start = max(1, int(parts[0].strip()))
                end = min(total, int(parts[1].strip()))
            else:
                start = max(1, int(pages))
                end = start
        except (ValueError, TypeError):
            start = 1
            end = min(total, 1)
        return start, end

    @staticmethod
    def guess_mime(path: Path) -> str:
        ext_map: dict[str, str] = {
            ".py": "text/x-python",
            ".js": "text/javascript",
            ".ts": "text/typescript",
            ".json": "application/json",
            ".yaml": "text/yaml",
            ".yml": "text/yaml",
            ".md": "text/markdown",
            ".txt": "text/plain",
            ".html": "text/html",
            ".css": "text/css",
            ".sh": "text/x-shellscript",
            ".toml": "text/toml",
            ".xml": "text/xml",
            ".csv": "text/csv",
            ".sql": "text/x-sql",
            ".rs": "text/x-rust",
            ".go": "text/x-go",
            ".java": "text/x-java",
            ".c": "text/x-c",
            ".cpp": "text/x-c++",
            ".h": "text/x-c",
            ".rb": "text/x-ruby",
            ".php": "text/x-php",
            ".swift": "text/x-swift",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
            ".zip": "application/zip",
            ".gz": "application/gzip",
        }
        return ext_map.get(path.suffix.lower(), "application/octet-stream")

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "list_directory",
        "List files/directories at a path.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    async def list_directory(path: str) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(target):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))
        if not target.is_dir():
            return Policy.json_error("not_a_directory", path=str(target))

        entries: list[dict[str, object]] = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name):
                try:
                    size = child.stat().st_size if child.is_file() else 0
                except OSError:
                    size = 0
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "is_dir": child.is_dir(),
                        "is_file": child.is_file(),
                        "size": size,
                    },
                )
        except PermissionError:
            return Policy.json_error("permission_denied", path=str(target))
        return json.dumps({"ok": True, "path": str(target), "entries": entries})

    @staticmethod
    @tool(
        "read_text_file",
        (
            "Read a file. Supports text, images (PNG/JPG/GIF/WebP as base64), "
            "PDFs (text extraction), and Jupyter notebooks (.ipynb cell parsing). "
            "Use offset/limit for large text files."
        ),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {
                    "type": "integer",
                    "description": "Max bytes for text files (default 200K).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed).",
                },
                "limit": {"type": "integer", "description": "Number of lines to read."},
                "pages": {
                    "type": "string",
                    "description": "Page range for PDFs (e.g. '1-5', '3', '10-20').",
                },
            },
            "required": ["path"],
        },
        output_schema={
            "x-output-levels": {
                "minimal": ["ok", "kind"],
                "standard": ["ok", "kind", "path", "text"],
                "full": [
                    "ok",
                    "kind",
                    "path",
                    "text",
                    "line_count",
                    "total_lines",
                    "base64",
                    "media_type",
                    "cells",
                    "pages_read",
                    "total_pages",
                ],
            },
            "x-default-level": "standard",
        },
    )
    async def read_text_file(
        path: str,
        max_bytes: int = 200_000,
        offset: int = 0,
        limit: int = 0,
        pages: str = "",
    ) -> str:
        from obscura.tools.system.file_state import is_unchanged, record_read

        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(target):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))
        if not target.is_file():
            return Policy.json_error("not_a_file", path=str(target))

        # Mtime-based read dedup: skip re-reading unchanged files.
        read_offset = offset if offset > 0 else None
        read_limit = limit if limit > 0 else None
        if is_unchanged(target, offset=read_offset, limit=read_limit):
            return json.dumps({"ok": True, "kind": "file_unchanged", "path": str(target)})

        suffix = target.suffix.lower()

        # --- Image files ---
        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        if suffix in _IMAGE_EXTS:
            import base64 as _b64

            media_map = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }
            data = target.read_bytes()
            encoded = _b64.b64encode(data).decode("ascii")
            record_read(target, offset=read_offset, limit=read_limit)
            return json.dumps(
                {
                    "ok": True,
                    "kind": "image",
                    "path": str(target),
                    "media_type": media_map.get(suffix, "application/octet-stream"),
                    "base64": encoded,
                    "size_bytes": len(data),
                },
            )

        # --- PDF files ---
        if suffix == ".pdf":
            try:
                import pdfplumber  # pyright: ignore[reportMissingImports]
            except ImportError:
                return Policy.json_error(
                    "missing_dependency",
                    detail="PDF reading requires pdfplumber. Install with: uv pip install pdfplumber",
                )
            try:
                pdf_module: Any = pdfplumber
                with pdf_module.open(target) as pdf:
                    pdf_any: Any = pdf
                    total_pages: int = len(pdf_any.pages)
                    # Parse page range.
                    if pages:
                        start_page, end_page = FsRead.parse_page_range(pages, total_pages)
                    else:
                        start_page, end_page = 1, min(total_pages, 20)
                    extracted: list[str] = []
                    for i in range(start_page - 1, end_page):
                        page_text: str = pdf_any.pages[i].extract_text() or ""
                        extracted.append(page_text)
                    text = "\n\n--- Page Break ---\n\n".join(extracted)
            except Exception as exc:
                return Policy.json_error("pdf_read_error", detail=str(exc))
            record_read(target, offset=read_offset, limit=read_limit)
            return json.dumps(
                {
                    "ok": True,
                    "kind": "pdf",
                    "path": str(target),
                    "text": text,
                    "pages_read": f"{start_page}-{end_page}",
                    "total_pages": total_pages,
                },
            )

        # --- Jupyter notebooks ---
        if suffix == ".ipynb":
            try:
                nb_data = json.loads(target.read_text(encoding="utf-8"))
                cells = nb_data.get("cells", [])
                parsed_cells: list[dict[str, Any]] = []
                for idx, cell in enumerate(cells):
                    source = "".join(cell.get("source", []))
                    cell_type = cell.get("cell_type", "code")
                    outputs: list[str] = []
                    for out in cell.get("outputs", []):
                        if "text" in out:
                            outputs.append("".join(out["text"]))
                        elif "data" in out and "text/plain" in out["data"]:
                            outputs.append("".join(out["data"]["text/plain"]))
                    parsed_cells.append(
                        {
                            "index": idx,
                            "cell_type": cell_type,
                            "source": source,
                            "outputs": outputs,
                        },
                    )
            except Exception as exc:
                return Policy.json_error("notebook_parse_error", detail=str(exc))
            record_read(target, offset=read_offset, limit=read_limit)
            return json.dumps(
                {
                    "ok": True,
                    "kind": "notebook",
                    "path": str(target),
                    "cell_count": len(parsed_cells),
                    "cells": parsed_cells,
                },
            )

        # --- Default: text files ---
        data = target.read_bytes()
        truncated = False
        if len(data) > max_bytes:
            data = data[:max_bytes]
            truncated = True
        text = data.decode("utf-8", errors="replace")

        # Line-based pagination via offset/limit.
        all_lines = text.splitlines(keepends=True)
        total_lines = len(all_lines)
        if offset > 0 or limit > 0:
            start = max(0, offset - 1) if offset > 0 else 0
            end = (start + limit) if limit > 0 else total_lines
            selected = all_lines[start:end]
            # Add line numbers.
            numbered = "".join(f"{start + i + 1:>6}\t{ln}" for i, ln in enumerate(selected))
            text = numbered
            truncated = end < total_lines

        # Apply token budget.
        from obscura.core.context_window import (
            MAX_FILE_READ_TOKENS,
            truncate_to_token_budget,
        )

        text, token_truncated = truncate_to_token_budget(text, MAX_FILE_READ_TOKENS)
        truncated = truncated or token_truncated

        record_read(target, offset=read_offset, limit=read_limit)
        return json.dumps(
            {
                "ok": True,
                "kind": "text",
                "path": str(target),
                "text": text,
                "truncated": truncated,
                "bytes_read": len(data),
                "total_lines": total_lines,
            },
        )

    @staticmethod
    @tool(
        "find_files",
        "Find files by glob pattern or name. Returns matching file paths with metadata.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory).",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '*.py', '**/*.ts').",
                },
                "name": {
                    "type": "string",
                    "description": "Exact or partial filename to match.",
                },
                "max_results": {"type": "integer"},
                "file_type": {"type": "string", "description": "'file', 'dir', or 'any'."},
            },
            "required": [],
        },
    )
    async def find_files(
        path: str = ".",
        pattern: str = "**/*",
        name: str = "",
        max_results: int = 200,
        file_type: str = "any",
    ) -> str:
        # Claude's native Glob tool accepts absolute patterns (e.g. "/abs/path/*.py"),
        # but pathlib.Path.glob rejects them. Split an absolute pattern into its
        # longest non-glob prefix (used as path) and the remaining relative pattern.
        if pattern and (pattern.startswith("/") or pattern.startswith("~")):
            from pathlib import PurePosixPath

            parts = PurePosixPath(pattern).parts
            base_parts: list[str] = []
            rel_parts: list[str] = []
            glob_chars = ("*", "?", "[")
            hit_glob = False
            for part in parts:
                if hit_glob or any(ch in part for ch in glob_chars):
                    hit_glob = True
                    rel_parts.append(part)
                else:
                    base_parts.append(part)
            if rel_parts:
                path = str(PurePosixPath(*base_parts)) if base_parts else "/"
                pattern = str(PurePosixPath(*rel_parts))
            else:
                # No glob chars — treat pattern as a literal absolute path lookup.
                path = str(PurePosixPath(*base_parts[:-1])) if len(base_parts) > 1 else "/"
                pattern = base_parts[-1] if base_parts else "**/*"

        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(target):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))
        if not target.is_dir():
            return Policy.json_error("not_a_directory", path=str(target))

        limit = max(1, min(max_results, 2000))

        # Cap the glob iterator to avoid unbounded traversal, then sort.
        _GLOB_CAP = limit * 10  # over-fetch so filtering still yields enough

        def _do_glob() -> list[Path]:
            out: list[Path] = []
            for fp in target.glob(pattern):
                out.append(fp)
                if len(out) >= _GLOB_CAP:
                    break
            return sorted(out)

        try:
            glob_paths = await asyncio.wait_for(
                asyncio.to_thread(_do_glob),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            return Policy.json_error(
                "timeout",
                path=str(target),
                detail="Glob timed out after 30s",
            )

        results: list[dict[str, object]] = []
        for fp in glob_paths:
            if len(results) >= limit:
                break
            if file_type == "file" and not fp.is_file():
                continue
            if file_type == "dir" and not fp.is_dir():
                continue
            if name and name.lower() not in fp.name.lower():
                continue
            try:
                st = fp.stat()
                results.append(
                    {
                        "path": str(fp),
                        "name": fp.name,
                        "is_dir": fp.is_dir(),
                        "size": st.st_size if fp.is_file() else 0,
                    },
                )
            except OSError:
                results.append(
                    {"path": str(fp), "name": fp.name, "is_dir": fp.is_dir(), "size": 0},
                )

        return json.dumps(
            {
                "ok": True,
                "path": str(target),
                "pattern": pattern,
                "count": len(results),
                "truncated": len(results) >= limit,
                "results": results,
            },
        )

    @staticmethod
    @tool(
        "file_info",
        "Get detailed file/directory metadata (size, permissions, timestamps, type).",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    async def file_info(path: str) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(target):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))

        st = target.stat()
        info: dict[str, object] = {
            "path": str(target),
            "name": target.name,
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
            "is_symlink": target.is_symlink(),
            "size": st.st_size,
            "permissions": stat_module.filemode(st.st_mode),
            "owner_uid": st.st_uid,
            "group_gid": st.st_gid,
            "created": st.st_ctime,
            "modified": st.st_mtime,
            "accessed": st.st_atime,
        }
        if target.is_symlink():
            try:
                info["symlink_target"] = str(target.readlink())
            except OSError:
                info["symlink_target"] = None
        if target.is_file():
            info["extension"] = target.suffix
            info["mime_guess"] = FsRead.guess_mime(target)

        return json.dumps({"ok": True, "info": info})

    @staticmethod
    @tool(
        "tree_directory",
        "Show a recursive directory tree with optional depth limit and file filters.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_depth": {
                    "type": "integer",
                    "description": "Max recursion depth (default 3).",
                },
                "include": {"type": "string", "description": "Glob filter for filenames."},
                "show_hidden": {"type": "boolean"},
                "max_entries": {"type": "integer"},
            },
            "required": ["path"],
        },
    )
    async def tree_directory(
        path: str,
        max_depth: int = 3,
        include: str = "",
        show_hidden: bool = False,
        max_entries: int = 500,
    ) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(target):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))
        if not target.is_dir():
            return Policy.json_error("not_a_directory", path=str(target))

        try:
            max_depth = int(max_depth)
        except (TypeError, ValueError):
            max_depth = 3
        try:
            max_entries = int(max_entries)
        except (TypeError, ValueError):
            max_entries = 500
        depth = max(1, min(max_depth, 10))
        limit = max(1, min(max_entries, 5000))
        lines: list[str] = [str(target)]
        count = 0

        def _walk(dir_path: Path, prefix: str, current_depth: int) -> None:
            nonlocal count
            if current_depth > depth or count >= limit:
                return
            try:
                children = sorted(
                    dir_path.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except PermissionError:
                return
            visible = [c for c in children if show_hidden or not c.name.startswith(".")]
            for i, child in enumerate(visible):
                if count >= limit:
                    return
                is_last = i == len(visible) - 1
                connector = "└── " if is_last else "├── "
                if child.is_file() and include and not fnmatch.fnmatch(child.name, include):
                    continue
                size_str = f" ({child.stat().st_size}B)" if child.is_file() else ""
                lines.append(f"{prefix}{connector}{child.name}{size_str}")
                count += 1
                if child.is_dir():
                    extension = "    " if is_last else "│   "
                    _walk(child, prefix + extension, current_depth + 1)

        _walk(target, "", 1)
        return json.dumps(
            {
                "ok": True,
                "path": str(target),
                "entries": count,
                "truncated": count >= limit,
                "tree": "\n".join(lines),
            },
        )

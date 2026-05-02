"""obscura.tools.result — Rich tool result builder for plugins and handlers.

Provides :class:`ToolResult`, a chainable builder that makes it trivial for
any tool handler (system or plugin) to produce structured, context-rich JSON
output consistent with Obscura's native tools.

Usage::

    from obscura.tools.result import ToolResult

    # Success with data
    return ToolResult.ok(action="query", source="duckdb") \\
        .data(columns=cols, rows=rows, row_count=len(rows)) \\
        .truncated(len(all_rows) > limit, full_count=len(all_rows)) \\
        .json()

    # Error
    return ToolResult.fail("auth_error", detail="Token expired") \\
        .context(url=url, method="GET") \\
        .json()

    # CLI wrapper
    return ToolResult.from_subprocess(proc, command=cmd) \\
        .json()

    # HTTP wrapper
    return await ToolResult.from_http(resp, url=url, method="GET") \\
        .json()

Every result includes ``ok: bool`` and contextual metadata.  Truncation,
timing, and error fields follow the same patterns as the native system tools.
"""

from __future__ import annotations

import json
import time
from typing import Any


class ToolResult:
    """Chainable builder for structured tool output.

    All ``set_*`` / ``data`` / ``context`` / ``truncated`` / ``timed``
    methods return ``self`` so calls can be chained fluently.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload: dict[str, Any] = payload or {}

    # -- Constructors -------------------------------------------------------

    @classmethod
    def ok(cls, **fields: Any) -> ToolResult:
        """Create a success result with optional initial fields."""
        return cls({"ok": True, **fields})

    @classmethod
    def fail(cls, error: str, **fields: Any) -> ToolResult:
        """Create an error result."""
        return cls({"ok": False, "error": error, **fields})

    @classmethod
    def from_subprocess(
        cls,
        proc: Any,
        *,
        command: str = "",
        cwd: str = "",
        max_output: int = 100_000,
    ) -> ToolResult:
        """Build a result from a completed ``subprocess.CompletedProcess``.

        Handles stdout/stderr truncation and adds line counts.
        """
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        stdout_truncated = len(stdout) > max_output
        stderr_truncated = len(stderr) > max_output

        r = cls(
            {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "command": command,
                "stdout": stdout[-max_output:] if stdout_truncated else stdout,
                "stderr": stderr[-max_output:] if stderr_truncated else stderr,
                "stdout_lines": stdout.count("\n") + (1 if stdout else 0),
            },
        )
        if cwd:
            r._payload["cwd"] = cwd
        if stdout_truncated:
            r._payload["stdout_truncated"] = True
            r._payload["stdout_full_size"] = len(stdout)
        if stderr_truncated:
            r._payload["stderr_truncated"] = True
            r._payload["stderr_full_size"] = len(stderr)
        return r

    @classmethod
    def from_http(
        cls,
        *,
        status_code: int,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        content_type: str = "",
        max_body: int = 500_000,
    ) -> ToolResult:
        """Build a result from HTTP response components.

        Handles body truncation and JSON auto-parse.
        """
        body_str = str(body) if body is not None else ""
        truncated = len(body_str) > max_body
        if truncated:
            body_str = body_str[:max_body]

        r = cls(
            {
                "ok": 200 <= status_code < 400,
                "status_code": status_code,
                "url": url,
                "method": method.upper(),
                "content_type": content_type,
                "body": body_str,
                "bytes_read": len(body_str),
                "truncated": truncated,
            },
        )
        if headers:
            r._payload["headers"] = headers

        # Auto-parse JSON bodies.
        if body and "json" in content_type.lower():
            try:
                if isinstance(body, str):
                    r._payload["json"] = json.loads(body)
                elif isinstance(body, dict | list):
                    r._payload["json"] = body
            except (json.JSONDecodeError, TypeError):
                pass

        return r

    # -- Chainable setters --------------------------------------------------

    def set(self, **fields: Any) -> ToolResult:
        """Set arbitrary fields on the result."""
        self._payload.update(fields)
        return self

    def context(self, **fields: Any) -> ToolResult:
        """Add contextual metadata (url, path, cwd, query, etc.)."""
        self._payload.update(fields)
        return self

    def data(self, **fields: Any) -> ToolResult:
        """Add primary data fields (results, rows, items, etc.)."""
        self._payload.update(fields)
        return self

    def truncated(
        self,
        is_truncated: bool = True,
        *,
        full_count: int | None = None,
        full_size: int | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        """Mark the result as truncated with optional size metadata."""
        self._payload["truncated"] = is_truncated
        if full_count is not None:
            self._payload["full_count"] = full_count
        if full_size is not None:
            self._payload["full_size"] = full_size
        if limit is not None:
            self._payload["limit"] = limit
        return self

    def timed(self, start: float) -> ToolResult:
        """Add duration_seconds computed from a ``time.monotonic()`` start."""
        self._payload["duration_seconds"] = round(time.monotonic() - start, 2)
        return self

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the result as a dict."""
        return dict(self._payload)

    def json(self) -> str:
        """Serialize to a JSON string (the return type expected by handlers)."""
        return json.dumps(self._payload)

    def __repr__(self) -> str:
        ok = self._payload.get("ok", "?")
        keys = ", ".join(sorted(self._payload.keys()))
        return f"<ToolResult ok={ok} keys=[{keys}]>"

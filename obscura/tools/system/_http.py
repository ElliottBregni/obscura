"""HTTP request and clipboard tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
from typing import Any
from urllib import error as url_error
from urllib import request as url_request

from obscura.core.tools import tool
from obscura.tools.system._policy import Policy
from obscura.tools.system._shell import Shell
import logging

logger = logging.getLogger(__name__)


class Http:
    """HTTP and clipboard tool namespace."""

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "download_file",
        "Download a file from a URL and save it to disk.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": "Local path to save the file.",
                },
                "timeout_seconds": {"type": "number"},
                "max_bytes": {
                    "type": "integer",
                    "description": "Max download size in bytes (default 50MB).",
                },
            },
            "required": ["url", "path"],
        },
    )
    async def download_file(
        url: str,
        path: str,
        timeout_seconds: float = 60.0,
        max_bytes: int = 50_000_000,
    ) -> str:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target
        ):
            return Policy.json_error("path_not_allowed", path=str(target))

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            url = Policy.validate_url(url)
        except ValueError as exc:
            logger.debug("suppressed exception in download_file", exc_info=True)
            return Policy.json_error("ssrf_blocked", url=url, detail=str(exc))
        req = url_request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Obscura/1.0)",
            },
        )

        try:
            with url_request.urlopen(req, timeout=timeout_seconds) as resp:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    return Policy.json_error(
                        "file_too_large", url=url, max_bytes=max_bytes
                    )
                target.write_bytes(data[:max_bytes])
                return json.dumps(
                    {
                        "ok": True,
                        "url": url,
                        "path": str(target),
                        "bytes_written": len(data),
                        "content_type": resp.headers.get("Content-Type", ""),
                    },
                )
        except url_error.HTTPError as exc:
            logger.debug("suppressed exception in download_file", exc_info=True)
            return Policy.json_error("download_failed", url=url, status=exc.code)
        except Exception as exc:
            logger.debug("suppressed exception in download_file", exc_info=True)
            return Policy.json_error("download_failed", url=url, detail=str(exc))

    @staticmethod
    @tool(
        "http_request",
        "Make an HTTP request (GET, POST, PUT, PATCH, DELETE) and return the response. Useful for REST API calls.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {
                    "type": "string",
                    "description": "HTTP method (default GET).",
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "body": {
                    "type": "string",
                    "description": "Request body (string or JSON).",
                },
                "json_body": {
                    "type": "object",
                    "description": "JSON body (auto-sets Content-Type).",
                },
                "timeout_seconds": {"type": "number"},
            },
            "required": ["url"],
        },
    )
    async def http_request(
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str = "",
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> str:
        req_headers = headers or {}
        payload: bytes | None = None

        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif body:
            payload = body.encode("utf-8")

        try:
            url = Policy.validate_url(url)
        except ValueError as exc:
            logger.debug("suppressed exception in http_request", exc_info=True)
            return Policy.json_error("ssrf_blocked", url=url, detail=str(exc))
        req = url_request.Request(
            url=url,
            method=method.upper(),
            headers=req_headers,
            data=payload,
        )

        _MAX_RESPONSE_BYTES = 500_000
        try:
            with url_request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                truncated = len(raw) > _MAX_RESPONSE_BYTES
                if truncated:
                    raw = raw[:_MAX_RESPONSE_BYTES]
                text = raw.decode("utf-8", errors="replace")
                response_headers = dict(resp.headers.items())
                content_type = response_headers.get("Content-Type", "")

                result: dict[str, object] = {
                    "ok": True,
                    "status": getattr(resp, "status", 200),
                    "url": url,
                    "method": method.upper(),
                    "content_type": content_type,
                    "headers": response_headers,
                    "body": text,
                    "bytes_read": len(raw),
                    "truncated": truncated,
                }

                # Try to parse JSON response
                if "json" in content_type.lower():
                    with contextlib.suppress(json.JSONDecodeError):
                        result["json"] = json.loads(text)

                return json.dumps(result)
        except url_error.HTTPError as exc:
            logger.debug("suppressed exception in http_request", exc_info=True)
            raw_error = exc.read(100_000)
            return json.dumps(
                {
                    "ok": False,
                    "status": exc.code,
                    "url": url,
                    "method": method.upper(),
                    "error": "http_error",
                    "body": raw_error.decode("utf-8", errors="replace"),
                    "headers": dict(exc.headers.items()) if exc.headers else {},
                },
            )
        except Exception as exc:
            logger.debug("suppressed exception in http_request", exc_info=True)
            return Policy.json_error(
                "request_failed",
                url=url,
                method=method.upper(),
                detail=str(exc),
            )

    @staticmethod
    @tool(
        "clipboard_read",
        "Read the current system clipboard contents (macOS only).",
        {
            "type": "object",
            "properties": {},
        },
    )
    async def clipboard_read() -> str:
        if platform.system() != "Darwin":
            return Policy.json_error(
                "clipboard_unsupported", platform=platform.system()
            )
        result = await Shell.run_command("pbpaste", timeout_seconds=5.0)
        payload = json.loads(result)
        if payload.get("ok"):
            return json.dumps(
                {
                    "ok": True,
                    "text": payload.get("stdout", ""),
                    "bytes": len(payload.get("stdout", "").encode("utf-8")),
                },
            )
        return result

    @staticmethod
    @tool(
        "clipboard_write",
        "Write text to the system clipboard (macOS only).",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to copy to clipboard."},
            },
            "required": ["text"],
        },
    )
    async def clipboard_write(text: str) -> str:
        if platform.system() != "Darwin":
            return Policy.json_error(
                "clipboard_unsupported", platform=platform.system()
            )
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=text.encode("utf-8")),
                timeout=5.0,
            )
        except TimeoutError:
            logger.debug("suppressed exception in clipboard_write", exc_info=True)
            proc.kill()
            await proc.wait()
            return Policy.json_error("timeout")
        if proc.returncode != 0:
            return Policy.json_error(
                "clipboard_write_failed",
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        return json.dumps(
            {
                "ok": True,
                "bytes_written": len(text.encode("utf-8")),
            },
        )

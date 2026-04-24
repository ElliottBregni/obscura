"""System command tools exposed to agent loops."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import fcntl
import fnmatch
import html as _html
import json
import os
import platform
import re
import shutil
import stat as stat_module
import subprocess
import sys
import time as _time
from pathlib import Path
from typing import Any, Literal, cast
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from obscura.core.tools import tool
from obscura.core.types import ToolSpec
from obscura.tools.system.delegation import (
    build_agent_cards_section,
    build_delegate_tool_spec,
)
from obscura.tools.system.intelligence import (
    causal_trace,
    context_snapshot,
    policy_probe,
)


def _strip_html(raw: str) -> str:
    """Strip HTML tags and decode entities, returning plain text."""
    # Drop script/style blocks entirely
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        "",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Replace block-level tags with newlines for readability
    text = re.sub(r"</(p|div|li|tr|h[1-6]|br)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode HTML entities (&amp; &lt; etc.)
    text = _html.unescape(text)
    # Collapse whitespace while preserving paragraph breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_DEFAULT_DENIED_COMMANDS: tuple[str, ...] = (
    "rm",
    "sudo",
    "shutdown",
    "reboot",
    "diskutil",
    "mkfs",
    "dd",
)


def _string_key_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    mapping = cast("dict[Any, Any]", value)
    return {str(key): item for key, item in mapping.items()}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _unsafe_full_access_enabled() -> bool:
    return _env_flag("OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS", default=False)


def _validate_url(url: str) -> str:
    """Validate a URL against SSRF attacks.

    - Only http:// and https:// schemes allowed.
    - DNS is resolved pre-flight; private/internal IPs are blocked.
    - Set OBSCURA_ALLOW_PRIVATE_URLS=true to bypass (dev/testing only).

    Returns the validated URL string.
    Raises ValueError on blocked URLs.
    """
    import ipaddress
    import socket

    if _env_flag("OBSCURA_ALLOW_PRIVATE_URLS", default=False):
        return url

    parsed = url_parse.urlparse(url)

    # --- Scheme check ---
    if parsed.scheme not in ("http", "https"):
        msg = (
            f"URL scheme {parsed.scheme!r} is not allowed. "
            "Only http:// and https:// URLs are permitted."
        )
        raise ValueError(msg)

    hostname = parsed.hostname
    if not hostname:
        msg = "URL has no hostname."
        raise ValueError(msg)

    # --- DNS resolution (pre-flight to defeat rebinding) ---
    try:
        addrinfos = socket.getaddrinfo(
            hostname, parsed.port or 443, proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        msg = f"DNS resolution failed for {hostname!r}: {exc}"
        raise ValueError(msg) from exc

    _BLOCKED_NETWORKS = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fd00::/8"),
        ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    )

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        addr = ipaddress.ip_address(ip_str)
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                msg = (
                    f"URL {url!r} resolves to private/internal address {ip_str} "
                    f"(in {net}). Request blocked to prevent SSRF. "
                    "Set OBSCURA_ALLOW_PRIVATE_URLS=true to override."
                )
                raise ValueError(msg)

    return url


def _normalize_list(values: str) -> set[str]:
    return {part.strip() for part in values.split(",") if part.strip()}


def _read_allowed_commands() -> set[str]:
    raw = os.environ.get("OBSCURA_SYSTEM_TOOLS_ALLOWED_COMMANDS", "")
    return _normalize_list(raw)


def _read_denied_commands() -> set[str]:
    if "OBSCURA_SYSTEM_TOOLS_DENIED_COMMANDS" in os.environ:
        raw = os.environ.get("OBSCURA_SYSTEM_TOOLS_DENIED_COMMANDS", "")
        return _normalize_list(raw)
    return set(_DEFAULT_DENIED_COMMANDS)


def _resolve_base_dir() -> Path | None:
    raw = os.environ.get("OBSCURA_SYSTEM_TOOLS_BASE_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _is_cwd_allowed(cwd: str) -> bool:
    base = _resolve_base_dir()
    if base is None:
        return True
    if not cwd:
        return True

    candidate = Path(cwd).expanduser().resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


def _resolve_path(path: str) -> Path:
    """Resolve a tool-provided file path.

    Absolute paths and ``~/`` paths are used as-is.  Relative paths are
    resolved against ``~/.obscura/output/`` (NOT the working directory)
    so that agent-generated files land in the Obscura data directory
    instead of polluting the project tree.

    Set ``OBSCURA_TOOLS_RELATIVE_TO_CWD=1`` to restore the old cwd
    behaviour when explicitly desired.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        import os

        if os.environ.get("OBSCURA_TOOLS_RELATIVE_TO_CWD", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            candidate = Path.cwd() / candidate
        else:
            from obscura.core.paths import resolve_obscura_output_dir

            candidate = resolve_obscura_output_dir() / candidate
    return candidate.resolve()


def _is_path_allowed(path: Path) -> bool:
    base = _resolve_base_dir()
    if base is None:
        return True
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _is_vault_write_allowed(path: Path) -> bool:
    """Return False if path is inside vault/user/ or vault/shared/ (read-only zones).

    vault/agent/ is the only zone that agents may write to.  Paths outside the
    vault entirely are unaffected and always return True.
    """
    from obscura.core.paths import resolve_obscura_home

    try:
        vault_root = resolve_obscura_home() / "vault"
        rel = path.resolve().relative_to(vault_root.resolve())
        # First component of the relative path is the zone name.
        zone = rel.parts[0] if rel.parts else ""
        if zone in ("user", "shared"):
            return False
    except (ValueError, Exception):
        pass  # Not inside vault — allow
    return True


def _json_error(error: str, **extra: object) -> str:
    payload: dict[str, object] = {"ok": False, "error": error, "exit_code": -1}
    payload.update(extra)
    return json.dumps(payload)


def _resolve_command(command: str) -> str:
    direct = shutil.which(command)
    if direct:
        return direct
    if command == "npx":
        nvm_root = Path.home() / ".nvm" / "versions" / "node"
        if nvm_root.is_dir():
            candidates = sorted(p for p in nvm_root.glob("*/bin/npx") if p.is_file())
            if candidates:
                return str(candidates[-1])
    return command


@tool(
    "run_python3",
    "Execute Python code using python3 -c and return stdout/stderr/exit_code.",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"},
        },
        "required": ["code"],
    },
    output_schema={
        "x-output-levels": {
            "minimal": ["ok", "exit_code"],
            "standard": ["ok", "stdout", "exit_code", "command", "cwd", "stdout_lines"],
            "full": [
                "ok",
                "stdout",
                "stderr",
                "exit_code",
                "command",
                "cwd",
                "stdout_lines",
            ],
        },
        "x-default-level": "standard",
    },
)
async def run_python3(
    code: str,
    cwd: str = "",
    timeout_seconds: float = 30.0,
) -> str:
    command = _resolve_command("python3")
    proc = await asyncio.create_subprocess_exec(
        command,
        "-c",
        code,
        cwd=(cwd or None),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return _json_error("timeout")
    stdout_str = stdout.decode("utf-8", errors="replace")
    stderr_str = stderr.decode("utf-8", errors="replace")
    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "command": "python3 -c <code>",
            "cwd": cwd or str(Path.cwd()),
            "stdout": stdout_str,
            "stderr": stderr_str,
            "stdout_lines": stdout_str.count("\n") + (1 if stdout_str else 0),
        },
    )


@tool(
    "run_command",
    "Execute a system command with args and return stdout/stderr/exit_code.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"},
        },
        "required": ["command"],
    },
    output_schema={
        "x-output-levels": {
            "minimal": ["ok", "exit_code"],
            "standard": ["ok", "stdout", "exit_code", "command", "cwd", "stdout_lines"],
            "full": [
                "ok",
                "stdout",
                "stderr",
                "exit_code",
                "command",
                "args",
                "cwd",
                "stdout_lines",
            ],
        },
        "x-default-level": "standard",
    },
)
async def run_command(
    command: str,
    args: list[str] | None = None,
    cwd: str = "",
    timeout_seconds: float = 60.0,
) -> str:
    normalized_command = command.strip()
    if not normalized_command:
        return _json_error("empty_command")

    if not _unsafe_full_access_enabled():
        allowed_commands = _read_allowed_commands()
        denied_commands = _read_denied_commands()
        if allowed_commands and normalized_command not in allowed_commands:
            return _json_error("command_not_allowed", command=normalized_command)
        if normalized_command in denied_commands:
            return _json_error("command_denied", command=normalized_command)

        if not _is_cwd_allowed(cwd):
            return _json_error("cwd_not_allowed", cwd=cwd)

    resolved_command = _resolve_command(normalized_command)
    if shutil.which(resolved_command) is None and not Path(resolved_command).is_file():
        return _json_error("command_not_found", command=normalized_command)

    process_args = args or []
    proc = await asyncio.create_subprocess_exec(
        resolved_command,
        *process_args,
        cwd=(cwd or None),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return _json_error("timeout")

    stdout_str = stdout.decode("utf-8", errors="replace")
    stderr_str = stderr.decode("utf-8", errors="replace")
    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "command": normalized_command,
            "args": process_args,
            "cwd": cwd or str(Path.cwd()),
            "stdout": stdout_str,
            "stderr": stderr_str,
            "stdout_lines": stdout_str.count("\n") + (1 if stdout_str else 0),
        },
    )


@tool(
    "run_shell",
    (
        "Execute a shell command via /bin/zsh -lc and return stdout/stderr/exit_code. "
        "Set run_in_background=true for long-running commands; returns a task_id "
        "that can be checked later."
    ),
    {
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "Shell script to execute."},
            "command": {
                "type": "string",
                "description": "Alias for script (LLM compat).",
            },
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"},
            "description": {
                "type": "string",
                "description": "User-facing description of what this command does.",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Run async and return a task_id immediately.",
            },
        },
    },
    output_schema={
        "x-output-levels": {
            "minimal": ["ok", "exit_code"],
            "standard": [
                "ok",
                "stdout",
                "exit_code",
                "command",
                "cwd",
                "stdout_lines",
                "description",
            ],
            "full": [
                "ok",
                "stdout",
                "stderr",
                "exit_code",
                "command",
                "cwd",
                "stdout_lines",
                "description",
                "background",
                "task_id",
                "stdout_truncated",
                "stderr_truncated",
                "stdout_full_path",
                "stderr_full_path",
                "stdout_full_size",
                "stderr_full_size",
            ],
        },
        "x-default-level": "standard",
    },
)
async def run_shell(
    script: str = "",
    command: str = "",
    cwd: str = "",
    timeout_seconds: float = 60.0,
    description: str = "",
    run_in_background: bool = False,
) -> str:
    actual_script = script or command
    if not actual_script:
        return json.dumps({"ok": False, "error": "no_script_provided"})

    if run_in_background:
        from obscura.core.background_tasks import get_background_task_manager

        mgr = get_background_task_manager()
        task_id = await mgr.start(
            f"/bin/zsh -lc {_shell_quote(actual_script)}",
            cwd=cwd,
            timeout=float(timeout_seconds),
        )
        return json.dumps(
            {
                "ok": True,
                "background": True,
                "task_id": task_id,
                "command": actual_script[:200],
                "description": description,
            },
        )

    result_json = await run_command(
        "/bin/zsh",
        args=["-lc", actual_script],
        cwd=cwd,
        timeout_seconds=float(timeout_seconds),
    )

    # Post-process: add context and truncate large output.
    result = json.loads(result_json)
    result["command"] = actual_script
    result["cwd"] = cwd or str(Path.cwd())
    if description:
        result["description"] = description
    stdout_val = result.get("stdout", "")
    result["stdout_lines"] = stdout_val.count("\n") + (1 if stdout_val else 0)

    _MAX_INLINE_OUTPUT = 100_000  # 100KB
    for key in ("stdout", "stderr"):
        val = result.get(key, "")
        if len(val) > _MAX_INLINE_OUTPUT:
            # Persist full output to disk.
            output_dir = Path.home() / ".obscura" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            import hashlib

            h = hashlib.sha256(val.encode("utf-8")).hexdigest()[:12]
            output_path = output_dir / f"{key}_{h}.txt"
            output_path.write_text(val, encoding="utf-8")
            result[key] = val[:_MAX_INLINE_OUTPUT]
            result[f"{key}_truncated"] = True
            result[f"{key}_full_path"] = str(output_path)
            result[f"{key}_full_size"] = len(val)

    return json.dumps(result)


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell embedding."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


# Response cache for web_fetch: {(url, prompt): (timestamp, response_json)}
_web_fetch_cache: dict[tuple[str, str], tuple[float, str]] = {}
_WEB_FETCH_CACHE_TTL = 900.0  # 15 minutes


@tool(
    "web_fetch",
    (
        "Fetch a URL and return the page content. "
        "HTML is automatically converted to Markdown (or plain text as fallback). "
        "Provide a `prompt` describing what to extract. "
        "Results are cached for 15 minutes."
    ),
    {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "prompt": {
                "type": "string",
                "description": "What to extract or summarize from the page.",
            },
            "method": {"type": "string"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": {"type": "string"},
            "timeout_seconds": {"type": "number"},
            "max_bytes": {"type": "integer"},
        },
        "required": ["url"],
    },
)
async def web_fetch(
    url: str,
    prompt: str = "",
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str = "",
    timeout_seconds: float = 20.0,
    max_bytes: int = 200_000,
) -> str:
    # Check cache for GET requests.
    cache_key = (url, prompt)
    if method.upper() == "GET" and cache_key in _web_fetch_cache:
        cached_ts, cached_result = _web_fetch_cache[cache_key]
        if _time.time() - cached_ts < _WEB_FETCH_CACHE_TTL:
            result = json.loads(cached_result)
            result["cached"] = True
            return json.dumps(result)

    timeout_seconds = float(timeout_seconds)
    max_bytes = int(max_bytes)
    request_headers = headers or {}
    payload = body.encode("utf-8") if body else None
    try:
        url = _validate_url(url)
    except ValueError as exc:
        return _json_error("ssrf_blocked", url=url, detail=str(exc))
    req = url_request.Request(
        url=url,
        method=method.upper(),
        headers=request_headers,
        data=payload,
    )
    try:
        with url_request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            data = raw[:max_bytes]
            text = data.decode("utf-8", errors="replace")
            response_headers = dict(response.headers.items())
            content_type = response_headers.get("Content-Type", "").lower()
            is_html = "html" in content_type or text.lstrip().startswith("<")

            # Convert HTML to Markdown if available, else strip tags.
            body_text = _html_to_markdown(text) if is_html else text

            # Token budget truncation.
            from obscura.core.context_window import (
                MAX_WEB_FETCH_TOKENS,
                truncate_to_token_budget,
            )

            body_text, token_truncated = truncate_to_token_budget(
                body_text,
                MAX_WEB_FETCH_TOKENS,
            )
            truncated = truncated or token_truncated

            # Redirect detection.
            final_url = response.geturl()
            redirect_info: dict[str, object] = {}
            if final_url != url:
                from urllib.parse import urlparse

                orig_host = urlparse(url).hostname
                final_host = urlparse(final_url).hostname
                if orig_host != final_host:
                    redirect_info = {
                        "redirected": True,
                        "original_host": orig_host,
                        "final_host": final_host,
                        "warning": "Redirected to a different domain",
                    }

            result: dict[str, object] = {
                "ok": True,
                "url": url,
                "final_url": final_url,
                "status": getattr(response, "status", 200),
                "content_type": content_type,
                "body": body_text,
                "truncated": truncated,
                "bytes_read": len(data),
            }
            if prompt:
                result["prompt"] = prompt
            if redirect_info:
                result["redirect"] = redirect_info

            result_json = json.dumps(result)
            # Cache GET responses.
            if method.upper() == "GET":
                _web_fetch_cache[cache_key] = (_time.time(), result_json)
            return result_json
    except url_error.HTTPError as exc:
        raw_error = exc.read(max_bytes)
        return json.dumps(
            {
                "ok": False,
                "url": url,
                "status": exc.code,
                "error": "http_error",
                "body": raw_error.decode("utf-8", errors="replace"),
            },
        )
    except Exception as exc:
        return _json_error("web_fetch_failed", url=url, detail=str(exc))


def _html_to_markdown(html_text: str) -> str:
    """Convert HTML to Markdown using markdownify if available, else strip tags."""
    try:
        import markdownify

        return markdownify.markdownify(
            html_text,
            heading_style="ATX",
            strip=["img", "script", "style"],
        )
    except ImportError:
        return _strip_html(html_text)


@tool(
    "web_search",
    (
        "Search the web for a query and return concise result items. "
        "Optionally filter by allowed_domains or blocked_domains."
    ),
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only include results from these domains.",
            },
            "blocked_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exclude results from these domains.",
            },
        },
        "required": ["query"],
    },
)
async def web_search(
    query: str,
    max_results: int = 5,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    """Search the web via DuckDuckGo HTML scraping (no API key required)."""
    limit = max(1, min(int(max_results), 20))
    encoded = url_parse.quote_plus(query)
    endpoint = f"https://html.duckduckgo.com/html/?q={encoded}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://duckduckgo.com/",
    }

    # Fetch raw HTML directly (web_fetch strips tags, we need structure)
    try:
        req = url_request.Request(endpoint, headers=headers)
        with url_request.urlopen(req, timeout=20) as resp:
            raw_html = resp.read(500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        return _json_error("web_search_fetch_failed", detail=str(exc))

    def clean(s):
        return re.sub(r"<[^>]+>", "", _html.unescape(s)).strip()

    titles = [
        clean(t) for t in re.findall(r'class="result__a"[^>]*>(.*?)</a>', raw_html)
    ]
    snippets = [
        clean(s)
        for s in re.findall(
            r'class="result__snippet"[^>]*>(.*?)</span>',
            raw_html,
            re.DOTALL,
        )
    ]
    hrefs = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', raw_html)
    urls_fb = [
        clean(u)
        for u in re.findall(
            r'class="result__url"[^>]*>\s*(.*?)\s*</a>',
            raw_html,
            re.DOTALL,
        )
    ]

    items: list[dict[str, str]] = []
    for i, title in enumerate(titles):
        if len(items) >= limit:
            break
        if not title:
            continue
        href = hrefs[i] if i < len(hrefs) else ""
        # DDG wraps hrefs in a redirect — extract uddg param if present
        if "uddg=" in href:
            uddg_match = re.search(r"uddg=([^&]+)", href)
            href = url_parse.unquote_plus(uddg_match.group(1)) if uddg_match else href
        url = href or (urls_fb[i] if i < len(urls_fb) else "")
        snippet = snippets[i] if i < len(snippets) else ""

        # Domain filtering.
        if url and (allowed_domains or blocked_domains):
            try:
                from urllib.parse import urlparse as _urlparse

                domain = (_urlparse(url).hostname or "").lower()
            except Exception:
                domain = ""
            if allowed_domains and not any(
                domain.endswith(d.lower()) for d in allowed_domains
            ):
                continue
            if blocked_domains and any(
                domain.endswith(d.lower()) for d in blocked_domains
            ):
                continue

        items.append({"title": title, "url": url, "snippet": snippet})

    return json.dumps(
        {"ok": True, "query": query, "count": len(items), "results": items},
    )


@tool(
    "task",
    (
        "Delegate a sub-task to a local Obscura agent subprocess. "
        "Spawns 'obscura <prompt>' and returns the captured output. "
        "Use 'target' to specify a specialist hint (e.g. 'explore', 'bash'); "
        "omit for default."
    ),
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The task to delegate."},
            "target": {
                "type": "string",
                "description": "Optional agent type hint (e.g. 'explore', 'bash').",
            },
        },
        "required": ["prompt"],
    },
)
async def task(prompt: str, target: str = "", timeout_seconds: float = 120.0) -> str:
    obscura_bin = _resolve_command("obscura")
    # prompt is a positional argument; use -s for agent type hint
    cmd = [obscura_bin]
    if target:
        cmd += ["-s", f"You are a {target} specialist. Focus on {target} tasks."]
    cmd += ["--max-turns", "25", "--no-confirm", prompt]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return json.dumps({"ok": False, "error": "timeout", "prompt": prompt})
        output = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        return json.dumps(
            {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "result": output,
                "stderr": err,
                "prompt": prompt,
                "target": target,
            },
        )
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "error": "delegation_failed",
                "message": str(exc),
                "prompt": prompt,
                "target": target,
            },
        )


@tool(
    "which_command",
    "Resolve an executable path for a command name.",
    {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
)
async def which_command(command: str) -> str:
    normalized = command.strip()
    if not normalized:
        return _json_error("empty_command")
    resolved = _resolve_command(normalized)
    discovered = shutil.which(resolved)
    if discovered is None:
        return _json_error("command_not_found", command=normalized)
    return json.dumps(
        {
            "ok": True,
            "command": normalized,
            "path": discovered,
            "exists": True,
        },
    )


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
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_dir():
        return _json_error("not_a_directory", path=str(target))

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
        return _json_error("permission_denied", path=str(target))
    return json.dumps({"ok": True, "path": str(target), "entries": entries})


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

    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_file():
        return _json_error("not_a_file", path=str(target))

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
            import pdfplumber
        except ImportError:
            return _json_error(
                "missing_dependency",
                detail="PDF reading requires pdfplumber. Install with: uv pip install pdfplumber",
            )
        try:
            with pdfplumber.open(target) as pdf:
                total_pages = len(pdf.pages)
                # Parse page range.
                if pages:
                    start_page, end_page = _parse_page_range(pages, total_pages)
                else:
                    start_page, end_page = 1, min(total_pages, 20)
                extracted: list[str] = []
                for i in range(start_page - 1, end_page):
                    page_text = pdf.pages[i].extract_text() or ""
                    extracted.append(page_text)
                text = "\n\n--- Page Break ---\n\n".join(extracted)
        except Exception as exc:
            return _json_error("pdf_read_error", detail=str(exc))
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
            return _json_error("notebook_parse_error", detail=str(exc))
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


def _parse_page_range(pages: str, total: int) -> tuple[int, int]:
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
    from obscura.tools.system.diff_utils import compute_unified_diff
    from obscura.tools.system.file_state import check_staleness

    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not _is_vault_write_allowed(target):
        return _json_error(
            "vault_zone_readonly",
            path=str(target),
            detail="vault/user and vault/shared are read-only; write to vault/agent instead",
        )
    if target.exists() and target.is_dir():
        return _json_error("path_is_directory", path=str(target))
    if target.exists() and not overwrite:
        return _json_error("file_exists", path=str(target))

    is_new = not target.exists()
    original = ""

    if not is_new:
        # Staleness check for existing files.
        staleness_err = check_staleness(target)
        if staleness_err is not None:
            return _json_error("stale_file", path=str(target), detail=staleness_err)
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
    from obscura.tools.system.diff_utils import compute_unified_diff

    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not _is_vault_write_allowed(target):
        return _json_error(
            "vault_zone_readonly",
            path=str(target),
            detail="vault/user and vault/shared are read-only; write to vault/agent instead",
        )
    if target.exists() and target.is_dir():
        return _json_error("path_is_directory", path=str(target))

    is_new = not target.exists()
    original = ""
    if not is_new:
        try:
            original = target.read_text(encoding="utf-8")
        except OSError:
            original = ""

    try:
        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        return _json_error("append_failed", path=str(target), detail=str(exc))

    after = original + text
    diff = compute_unified_diff(original, after, str(target))
    total_lines = after.count("\n") + (1 if after and not after.endswith("\n") else 0)
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
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    try:
        target.mkdir(parents=parents, exist_ok=exist_ok)
    except OSError as exc:
        return _json_error("mkdir_failed", path=str(target), detail=str(exc))
    return json.dumps({"ok": True, "path": str(target)})


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
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not _is_vault_write_allowed(target):
        return _json_error(
            "vault_zone_readonly",
            path=str(target),
            detail="vault/user and vault/shared are read-only; write to vault/agent instead",
        )
    if not target.exists():
        if missing_ok:
            return json.dumps({"ok": True, "path": str(target), "removed": False})
        return _json_error("path_not_found", path=str(target))

    if target.is_dir():
        if not recursive:
            return _json_error("directory_requires_recursive_true", path=str(target))
        shutil.rmtree(target)
        return json.dumps({"ok": True, "path": str(target), "removed": True})

    target.unlink(missing_ok=missing_ok)
    return json.dumps({"ok": True, "path": str(target), "removed": True})


@tool(
    "get_environment",
    "Return environment variables (optionally filtered by prefix).",
    {
        "type": "object",
        "properties": {
            "prefix": {"type": "string"},
            "include_values": {"type": "boolean"},
        },
    },
)
async def get_environment(prefix: str = "", include_values: bool = False) -> str:
    selected: dict[str, str | None] = {}
    for key, value in sorted(os.environ.items()):
        if prefix and not key.startswith(prefix):
            continue
        selected[key] = value if include_values else None
    return json.dumps({"ok": True, "count": len(selected), "variables": selected})


@tool(
    "get_system_info",
    "Return host system information and common tool availability.",
    {
        "type": "object",
        "properties": {},
    },
)
async def get_system_info() -> str:
    info = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": sys.version,
        "cwd": str(Path.cwd()),
        "home": str(Path.home()),
        "commands": {
            "python3": shutil.which("python3"),
            "npx": _resolve_command("npx"),
            "node": shutil.which("node"),
            "git": shutil.which("git"),
            "uv": shutil.which("uv"),
        },
    }
    return json.dumps({"ok": True, "info": info})


@tool(
    "list_processes",
    "List running processes with pid/ppid/user/command.",
    {
        "type": "object",
        "properties": {},
    },
)
async def list_processes() -> str:
    return await run_command(
        "ps",
        args=["-ax", "-o", "pid,ppid,user,%cpu,%mem,command"],
        timeout_seconds=30.0,
    )


@tool(
    "signal_process",
    "Send a signal to a process id.",
    {
        "type": "object",
        "properties": {
            "pid": {"type": "integer"},
            "signal": {"type": "string"},
        },
        "required": ["pid"],
    },
)
async def signal_process(pid: int, signal: str = "TERM") -> str:
    return await run_command(
        "kill",
        args=[f"-{signal}", str(pid)],
        timeout_seconds=10.0,
    )


@tool(
    "list_listening_ports",
    "List listening TCP/UDP ports.",
    {
        "type": "object",
        "properties": {},
    },
)
async def list_listening_ports() -> str:
    if shutil.which("lsof"):
        return await run_command(
            "lsof",
            args=["-nP", "-iTCP", "-sTCP:LISTEN"],
            timeout_seconds=30.0,
        )
    if shutil.which("netstat"):
        return await run_command("netstat", args=["-an"], timeout_seconds=30.0)
    return _json_error("no_supported_port_tool", required_any=["lsof", "netstat"])


# ---------------------------------------------------------------------------
# File operation tools
# ---------------------------------------------------------------------------


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
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))

    # Legacy compat: max_results overrides head_limit when provided.
    effective_limit = max_results if max_results > 0 else head_limit
    file_type = type or kwargs.get("type", "")

    # Try ripgrep first, fall back to Python implementation.
    rg_path = shutil.which("rg")
    if rg_path is not None:
        return await _grep_via_ripgrep(
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

    return await _grep_via_python(
        pattern=pattern,
        target=target,
        include=include,
        case_sensitive=case_sensitive,
        output_mode=output_mode,
        head_limit=effective_limit,
        offset=offset,
    )


async def _grep_via_ripgrep(
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
        proc.kill()
        await proc.wait()
        return _json_error("timeout", detail="ripgrep timed out after 30s")

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
            matches.append({"file": parts[0], "line": parts[1], "text": parts[2][:500]})
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


async def _grep_via_python(
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
        return _json_error("invalid_regex", pattern=pattern, detail=str(exc))

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
            return
        file_match_count = 0
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                file_match_count += 1
                if output_mode == "content":
                    matches.append(
                        {"file": str(fp), "line": lineno, "text": line.rstrip()[:500]},
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
        except asyncio.TimeoutError:
            return _json_error(
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
        entries = [{"file": f, "count": c} for f, c in file_counts.items()]
        entries = entries[offset:]
        truncated = len(entries) > limit
        entries = entries[:limit]
        return json.dumps(
            {
                "ok": True,
                "mode": "count",
                "pattern": pattern,
                "path": str(target),
                "num_files": len(entries),
                "total_matches": sum(e["count"] for e in entries),
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

    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_dir():
        return _json_error("not_a_directory", path=str(target))

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
        return _json_error(
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
    from obscura.tools.system.diff_utils import compute_unified_diff
    from obscura.tools.system.file_state import check_staleness

    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not _is_vault_write_allowed(target):
        return _json_error(
            "vault_zone_readonly",
            path=str(target),
            detail="vault/user and vault/shared are read-only; write to vault/agent instead",
        )
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_file():
        return _json_error("not_a_file", path=str(target))

    # Acquire an exclusive advisory lock around the read-modify-write
    # cycle to prevent TOCTOU races with other agents/processes.
    with target.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            # Staleness check — must happen inside the lock so no
            # concurrent writer can slip in between check and read.
            staleness_err = check_staleness(target)
            if staleness_err is not None:
                return _json_error("stale_file", path=str(target), detail=staleness_err)

            content = fh.read()

            # Try exact match first, then quote-normalized fallback.
            actual_old = old_text
            if old_text not in content:
                normalized_old = _normalize_quotes(old_text)
                normalized_content = _normalize_quotes(content)
                if normalized_old in normalized_content:
                    # Find the actual substring in original content by position.
                    pos = normalized_content.index(normalized_old)
                    actual_old = content[pos : pos + len(old_text)]
                    if actual_old not in content:
                        return _json_error(
                            "text_not_found",
                            path=str(target),
                            old_text=old_text[:200],
                        )
                else:
                    return _json_error(
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


def _normalize_quotes(text: str) -> str:
    """Normalize curly/smart quotes to straight ASCII quotes."""
    replacements = {
        "\u2018": "'",
        "\u2019": "'",  # Single curly quotes
        "\u201c": '"',
        "\u201d": '"',  # Double curly quotes
        "\u2032": "'",
        "\u2033": '"',  # Prime marks
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


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
    src_path = _resolve_path(src)
    dst_path = _resolve_path(dst)
    if not _unsafe_full_access_enabled():
        if not _is_path_allowed(src_path):
            return _json_error("path_not_allowed", path=str(src_path))
        if not _is_path_allowed(dst_path):
            return _json_error("path_not_allowed", path=str(dst_path))
    if not src_path.exists():
        return _json_error("path_not_found", path=str(src_path))
    if dst_path.exists() and not overwrite:
        return _json_error("destination_exists", path=str(dst_path))

    if src_path.is_dir():
        if dst_path.exists():
            shutil.rmtree(dst_path)
        shutil.copytree(src_path, dst_path)
    else:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)

    return json.dumps({"ok": True, "src": str(src_path), "dst": str(dst_path)})


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
    src_path = _resolve_path(src)
    dst_path = _resolve_path(dst)
    if not _unsafe_full_access_enabled():
        if not _is_path_allowed(src_path):
            return _json_error("path_not_allowed", path=str(src_path))
        if not _is_path_allowed(dst_path):
            return _json_error("path_not_allowed", path=str(dst_path))
    if not src_path.exists():
        return _json_error("path_not_found", path=str(src_path))
    if dst_path.exists() and not overwrite:
        return _json_error("destination_exists", path=str(dst_path))

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_path), str(dst_path))
    return json.dumps({"ok": True, "src": str(src_path), "dst": str(dst_path)})


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
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))

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
        info["mime_guess"] = _guess_mime(target)

    return json.dumps({"ok": True, "info": info})


def _guess_mime(path: Path) -> str:
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
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_dir():
        return _json_error("not_a_directory", path=str(target))

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
    path_a = _resolve_path(file_a)
    path_b = _resolve_path(file_b)
    if not _unsafe_full_access_enabled():
        if not _is_path_allowed(path_a):
            return _json_error("path_not_allowed", path=str(path_a))
        if not _is_path_allowed(path_b):
            return _json_error("path_not_allowed", path=str(path_b))
    if not path_a.exists():
        return _json_error("path_not_found", path=str(path_a))
    if not path_b.exists():
        return _json_error("path_not_found", path=str(path_b))

    try:
        lines_a = path_a.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True,
        )
        lines_b = path_b.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True,
        )
    except OSError as exc:
        return _json_error("read_failed", detail=str(exc))

    try:
        context_lines = int(context_lines)
    except (TypeError, ValueError):
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


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------


async def _git(args: list[str], cwd: str = "", timeout: float = 30.0) -> dict[str, Any]:
    """Run a git command and return parsed result."""
    git_cmd = shutil.which("git")
    if git_cmd is None:
        return {"ok": False, "error": "git_not_found"}
    work_dir = cwd or str(Path.cwd())
    proc = await asyncio.create_subprocess_exec(
        git_cmd,
        *args,
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "timeout", "git_args": args, "cwd": work_dir}
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    # Git writes many confirmations (commit, push, branch) to stderr.
    # Merge into stdout so the LLM backend always sees the output.
    combined = out or err if proc.returncode == 0 else out
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "git_command": f"git {' '.join(args)}",
        "cwd": work_dir,
        "stdout": combined,
        "stderr": err,
    }


@tool(
    "git",
    "Unified git operations: status, diff, log, commit, branch, push, tag.",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "diff",
                    "log",
                    "commit",
                    "branch",
                    "push",
                    "tag",
                ],
                "description": "Git operation to perform.",
            },
            "message": {
                "type": "string",
                "description": "Commit message (commit) or tag annotation (tag create).",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to stage (commit). Use ['.'] for all changes.",
            },
            "ref": {
                "type": "string",
                "description": "Ref/branch/tag name. Used by diff, log, branch, tag.",
            },
            "remote": {
                "type": "string",
                "description": "Remote name for push (default 'origin').",
            },
            "sub_action": {
                "type": "string",
                "description": "Sub-operation: branch='list|create|switch', tag='list|create|delete'.",
            },
            "path": {
                "type": "string",
                "description": "File/dir path filter (diff, log).",
            },
            "staged": {
                "type": "boolean",
                "description": "Show staged changes (diff --cached).",
            },
            "stat_only": {
                "type": "boolean",
                "description": "Show diffstat only (diff --stat).",
            },
            "short": {
                "type": "boolean",
                "description": "Short format (status, default true).",
            },
            "max_count": {
                "type": "integer",
                "description": "Number of commits (log, default 10).",
            },
            "oneline": {
                "type": "boolean",
                "description": "One-line format (log, default true).",
            },
            "author": {
                "type": "string",
                "description": "Filter by author (log).",
            },
            "since": {
                "type": "string",
                "description": "Show commits after date (log, e.g. '2024-01-01').",
            },
            "set_upstream": {
                "type": "boolean",
                "description": "Set upstream tracking (push -u).",
            },
            "push_tags": {
                "type": "boolean",
                "description": "Push all tags (push --tags).",
            },
            "cwd": {"type": "string"},
        },
        "required": ["action"],
    },
)
async def git(  # noqa: C901 — unified dispatch, complexity is expected
    action: str,
    message: str = "",
    files: list[str] | None = None,
    ref: str = "",
    remote: str = "origin",
    sub_action: str = "list",
    path: str = "",
    staged: bool = False,
    stat_only: bool = False,
    short: bool = True,
    max_count: int = 10,
    oneline: bool = True,
    author: str = "",
    since: str = "",
    set_upstream: bool = False,
    push_tags: bool = False,
    cwd: str = "",
) -> str:
    # -- status --
    if action == "status":
        args = ["status"]
        if short:
            args.append("--short")
        args.append("--branch")
        return json.dumps(await _git(args, cwd=cwd))

    # -- diff --
    if action == "diff":
        args = ["diff"]
        if staged:
            args.append("--cached")
        if stat_only:
            args.append("--stat")
        if ref:
            args.append(ref)
        if path:
            args.extend(["--", path])
        result = await _git(args, cwd=cwd)
        if result.get("ok") and len(result.get("stdout", "")) > 100_000:
            result["stdout"] = result["stdout"][:100_000] + "\n... (truncated)"
            result["truncated"] = True
        return json.dumps(result)

    # -- log --
    if action == "log":
        try:
            max_count = int(max_count)
        except (TypeError, ValueError):
            max_count = 10
        count = max(1, min(max_count, 100))
        args = ["log", f"-{count}"]
        if oneline:
            args.append("--oneline")
        else:
            args.extend(["--format=%H %an %ae %ai%n%s%n%b---"])
        if author:
            args.append(f"--author={author}")
        if since:
            args.append(f"--since={since}")
        if ref:
            args.append(ref)
        if path:
            args.extend(["--", path])
        return json.dumps(await _git(args, cwd=cwd))

    # -- commit --
    if action == "commit":
        if not message.strip():
            return _json_error("empty_commit_message")
        stage_files = files or ["."]
        add_result = await _git(["add", *stage_files], cwd=cwd)
        if not add_result.get("ok"):
            return json.dumps(add_result)
        return json.dumps(await _git(["commit", "-m", message], cwd=cwd))

    # -- branch --
    if action == "branch":
        if sub_action == "list":
            return json.dumps(
                await _git(["branch", "-a", "--no-color"], cwd=cwd),
            )
        if sub_action == "create":
            if not ref.strip():
                return _json_error("branch_name_required")
            return json.dumps(await _git(["checkout", "-b", ref], cwd=cwd))
        if sub_action == "switch":
            if not ref.strip():
                return _json_error("branch_name_required")
            return json.dumps(await _git(["checkout", ref], cwd=cwd))
        return _json_error(
            "invalid_sub_action",
            sub_action=sub_action,
            valid=["list", "create", "switch"],
        )

    # -- push --
    if action == "push":
        args = ["push"]
        if set_upstream:
            args.append("-u")
        args.append(remote)
        if ref:
            args.append(ref)
        if push_tags:
            args.append("--tags")
        return json.dumps(await _git(args, cwd=cwd, timeout=60.0))

    # -- tag --
    if action == "tag":
        if sub_action == "list":
            return json.dumps(
                await _git(["tag", "-l", "--sort=-creatordate"], cwd=cwd),
            )
        if sub_action == "create":
            if not ref.strip():
                return _json_error("tag_name_required")
            args = ["tag"]
            if message:
                args.extend(["-a", ref, "-m", message])
            else:
                args.append(ref)
            return json.dumps(await _git(args, cwd=cwd))
        if sub_action == "delete":
            if not ref.strip():
                return _json_error("tag_name_required")
            return json.dumps(await _git(["tag", "-d", ref], cwd=cwd))
        return _json_error(
            "invalid_sub_action",
            sub_action=sub_action,
            valid=["list", "create", "delete"],
        )

    return _json_error(
        "invalid_action",
        action=action,
        valid=["status", "diff", "log", "commit", "branch", "push", "tag"],
    )


# ---------------------------------------------------------------------------
# Utility tools
# ---------------------------------------------------------------------------


@tool(
    "download_file",
    "Download a file from a URL and save it to disk.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "path": {"type": "string", "description": "Local path to save the file."},
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
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        url = _validate_url(url)
    except ValueError as exc:
        return _json_error("ssrf_blocked", url=url, detail=str(exc))
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
                return _json_error("file_too_large", url=url, max_bytes=max_bytes)
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
        return _json_error("download_failed", url=url, status=exc.code)
    except Exception as exc:
        return _json_error("download_failed", url=url, detail=str(exc))


@tool(
    "http_request",
    "Make an HTTP request (GET, POST, PUT, PATCH, DELETE) and return the response. Useful for REST API calls.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "description": "HTTP method (default GET)."},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": {"type": "string", "description": "Request body (string or JSON)."},
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
        url = _validate_url(url)
    except ValueError as exc:
        return _json_error("ssrf_blocked", url=url, detail=str(exc))
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
        return _json_error(
            "request_failed",
            url=url,
            method=method.upper(),
            detail=str(exc),
        )


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
        return _json_error("clipboard_unsupported", platform=platform.system())
    result = await run_command("pbpaste", timeout_seconds=5.0)
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
        return _json_error("clipboard_unsupported", platform=platform.system())
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
        proc.kill()
        await proc.wait()
        return _json_error("timeout")
    if proc.returncode != 0:
        return _json_error(
            "clipboard_write_failed",
            stderr=stderr.decode("utf-8", errors="replace"),
        )
    return json.dumps(
        {
            "ok": True,
            "bytes_written": len(text.encode("utf-8")),
        },
    )


@tool(
    "json_query",
    "Query a JSON file or string using dot-notation paths (e.g. 'data.users[0].name').",
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to a JSON file (optional if data provided).",
            },
            "data": {"type": "string", "description": "Raw JSON string to query."},
            "query": {
                "type": "string",
                "description": "Dot-notation path (e.g. 'users[0].name', 'config.database.host').",
            },
            "keys_only": {
                "type": "boolean",
                "description": "Return only keys at the query path.",
            },
        },
        "required": ["query"],
    },
)
async def json_query(
    query: str,
    path: str = "",
    data: str = "",
    keys_only: bool = False,
) -> str:
    # Load JSON
    if path:
        target = _resolve_path(path)
        if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
            return _json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return _json_error("path_not_found", path=str(target))
        try:
            raw = target.read_text(encoding="utf-8")
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _json_error("invalid_json", path=str(target), detail=str(exc))
    elif data:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            return _json_error("invalid_json", detail=str(exc))
    else:
        return _json_error("no_input", detail="Provide either path or data.")

    # Navigate the query path
    current: Any = obj
    parts = _parse_json_path(query)
    for part in parts:
        try:
            if isinstance(part, int) or isinstance(current, dict):
                current = current[part]
            elif isinstance(current, list):
                current = current[int(part)]
            else:
                return _json_error("invalid_path", query=query, at=part)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            return _json_error(
                "path_not_found_in_data",
                query=query,
                at=part,
                detail=str(exc),
            )

    if keys_only and isinstance(current, dict):
        return json.dumps({"ok": True, "query": query, "keys": list(current.keys())})

    # Serialize the result
    try:
        result_str = json.dumps(current)
    except (TypeError, ValueError):
        result_str = str(current)

    return json.dumps(
        {
            "ok": True,
            "query": query,
            "result": current
            if isinstance(current, (str, int, float, bool, type(None), list, dict))
            else result_str,
        },
    )


def _parse_json_path(query: str) -> list[str | int]:
    """Parse a dot-notation JSON path like 'users[0].name' into parts."""
    parts: list[str | int] = []
    for segment in query.split("."):
        if not segment:
            continue
        # Handle array indices: users[0] → "users", 0
        bracket_match = re.match(r"^(\w+)\[(\d+)\]$", segment)
        if bracket_match:
            parts.append(bracket_match.group(1))
            parts.append(int(bracket_match.group(2)))
        elif segment.isdigit():
            parts.append(int(segment))
        else:
            parts.append(segment)
    return parts


# ---------------------------------------------------------------------------
# Dynamic tool creation + code sandbox
# ---------------------------------------------------------------------------

# In-memory store for dynamically created tools (session-scoped).
_dynamic_tools: dict[str, ToolSpec] = {}


@tool(
    "create_tool",
    (
        "Dynamically create a new tool at runtime. Write a Python function body "
        "that accepts keyword arguments and returns a JSON string. The tool becomes "
        "immediately available for subsequent calls in this session."
    ),
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Tool name (lowercase, underscored).",
            },
            "description": {"type": "string", "description": "What the tool does."},
            "parameters": {
                "type": "object",
                "description": "JSON Schema for tool parameters.",
            },
            "code": {
                "type": "string",
                "description": (
                    "Python function body. Receives kwargs matching the parameters schema. "
                    "Must return a JSON string. Has access to: json, os, re, pathlib.Path, "
                    'asyncio, subprocess, urllib. Example: \'return json.dumps({"ok": True, "result": kwargs["x"] * 2})\''
                ),
            },
        },
        "required": ["name", "description", "code"],
    },
)
async def create_tool(
    name: str,
    description: str,
    code: str,
    parameters: dict[str, Any] | None = None,
) -> str:
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    clean_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
    if not clean_name:
        return _json_error("invalid_tool_name")
    if clean_name in {s.name for s in get_system_tool_specs()}:
        return _json_error("name_conflicts_with_builtin", name=clean_name)

    # Build the async handler function
    # Available imports inside the sandbox
    _SAFE_BUILTINS: dict[str, Any] = {
        "len": len, "range": range, "enumerate": enumerate, "zip": zip,
        "map": map, "filter": filter, "sorted": sorted, "reversed": reversed,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
        "str": str, "int": int, "float": float, "bool": bool,
        "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
        "print": print, "type": type,
        "None": None, "True": True, "False": False,
        "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
        "any": any, "all": all,
        "ValueError": ValueError, "TypeError": TypeError,
        "KeyError": KeyError, "RuntimeError": RuntimeError,
        "Exception": Exception,
    }
    sandbox_globals: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "json": json,
        "re": re,
        "Path": Path,
        "asyncio": asyncio,
        "base64": __import__("base64"),
        "time": _time,
    }

    # Wrap user code in an async function
    indented_code = "\n".join(f"    {line}" for line in code.splitlines())
    func_source = f"async def _dynamic_handler(**kwargs):\n{indented_code}"

    try:
        exec(func_source, sandbox_globals)  # noqa: S102
    except SyntaxError as exc:
        return _json_error("syntax_error", detail=str(exc), line=exc.lineno)

    handler = sandbox_globals["_dynamic_handler"]

    # Create and store the ToolSpec
    spec = ToolSpec(
        name=clean_name,
        description=description,
        parameters=parameters or {"type": "object", "properties": {}},
        handler=handler,
    )
    _dynamic_tools[clean_name] = spec

    return json.dumps(
        {
            "ok": True,
            "name": clean_name,
            "description": description,
            "message": f"Tool '{clean_name}' created. Call it with the tool name '{clean_name}'.",
        },
    )


@tool(
    "call_dynamic_tool",
    "Call a dynamically created tool by name.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the dynamic tool."},
            "args": {"type": "object", "description": "Arguments to pass as kwargs."},
        },
        "required": ["name"],
    },
)
async def call_dynamic_tool(name: str, args: dict[str, Any] | None = None) -> str:
    clean_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
    spec = _dynamic_tools.get(clean_name)
    if spec is None:
        available = list(_dynamic_tools.keys())
        return _json_error(
            "dynamic_tool_not_found",
            name=clean_name,
            available=available,
        )

    kwargs = args or {}
    try:
        result = await spec.handler(**kwargs)
        if isinstance(result, str):
            return result
        return json.dumps({"ok": True, "result": result})
    except Exception as exc:
        return _json_error("dynamic_tool_error", name=clean_name, detail=str(exc))


@tool(
    "list_dynamic_tools",
    "List all dynamically created tools in this session.",
    {
        "type": "object",
        "properties": {},
    },
)
async def list_dynamic_tools() -> str:
    tools = [
        {"name": name, "description": spec.description}
        for name, spec in _dynamic_tools.items()
    ]
    return json.dumps({"ok": True, "count": len(tools), "tools": tools})


@tool(
    "code_sandbox",
    (
        "Execute code in a sandboxed environment with timeout and resource limits. "
        "Supports Python, Node.js, and shell scripts. Captures stdout, stderr, "
        "and exit code. Files created in the sandbox persist for the session. "
        "Use for writing and testing code, running scripts, or prototyping."
    ),
    {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "'python', 'node', 'bash', or 'zsh'.",
            },
            "code": {"type": "string", "description": "Source code to execute."},
            "timeout_seconds": {"type": "number"},
            "cwd": {
                "type": "string",
                "description": "Working directory for execution.",
            },
            "stdin": {"type": "string", "description": "Text to pipe to stdin."},
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Extra environment variables.",
            },
            "save_as": {
                "type": "string",
                "description": "Save the code to this file path before executing.",
            },
        },
        "required": ["language", "code"],
    },
)
async def code_sandbox(
    language: str,
    code: str,
    timeout_seconds: float = 30.0,
    cwd: str = "",
    stdin: str = "",
    env: dict[str, str] | None = None,
    save_as: str = "",
) -> str:
    lang = language.strip().lower()
    timeout_seconds = max(1.0, min(float(timeout_seconds), 300.0))

    # Resolve interpreter
    interpreters: dict[str, tuple[str, list[str]]] = {
        "python": ("python3", ["-c", code]),
        "python3": ("python3", ["-c", code]),
        "node": ("node", ["-e", code]),
        "nodejs": ("node", ["-e", code]),
        "javascript": ("node", ["-e", code]),
        "js": ("node", ["-e", code]),
        "bash": ("/bin/bash", ["-c", code]),
        "zsh": ("/bin/zsh", ["-c", code]),
        "sh": ("/bin/sh", ["-c", code]),
        "shell": ("/bin/zsh", ["-lc", code]),
    }

    if lang not in interpreters:
        return _json_error(
            "unsupported_language",
            language=lang,
            supported=list(interpreters.keys()),
        )

    cmd, args = interpreters[lang]
    resolved_cmd = _resolve_command(cmd)

    # Optionally save code to file first
    if save_as:
        save_path = _resolve_path(save_as)
        if not _unsafe_full_access_enabled() and not _is_path_allowed(save_path):
            return _json_error("path_not_allowed", path=str(save_path))
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(code, encoding="utf-8")
        # Execute the file instead of -c
        if (
            lang in ("python", "python3")
            or lang in ("node", "nodejs", "javascript", "js")
            or lang in ("bash", "zsh", "sh", "shell")
        ):
            args = [str(save_path)]

    # Build environment
    run_env = dict(os.environ)
    if env:
        run_env.update(env)

    proc = await asyncio.create_subprocess_exec(
        resolved_cmd,
        *args,
        cwd=(cwd or None),
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=run_env,
    )

    try:
        input_data = stdin.encode("utf-8") if stdin else None
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return json.dumps(
            {
                "ok": False,
                "error": "timeout",
                "language": lang,
                "timeout_seconds": timeout_seconds,
            },
        )

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")

    result: dict[str, object] = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "language": lang,
        "stdout": stdout_text[:100_000],
        "stderr": stderr_text[:50_000],
    }
    if save_as:
        result["saved_to"] = str(_resolve_path(save_as))
    if len(stdout_text) > 100_000:
        result["stdout_truncated"] = True
    if len(stderr_text) > 50_000:
        result["stderr_truncated"] = True

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Copilot GPT-5 Mini query
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Context window awareness
# ---------------------------------------------------------------------------

# Session-level token usage tracker.  Updated by the agent loop after each
# turn (via the ``update_token_usage`` helper) so the LLM can introspect.
_token_usage: dict[str, int] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "context_window": 0,
    "compact_threshold": 0,
}


def update_token_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    context_window: int = 0,
    compact_threshold: int = 0,
) -> None:
    """Called by the agent loop / CLI to keep the token tracker current."""
    if input_tokens:
        _token_usage["input_tokens"] = input_tokens
    if output_tokens:
        _token_usage["output_tokens"] = output_tokens
    _token_usage["total_tokens"] = (
        _token_usage["input_tokens"] + _token_usage["output_tokens"]
    )
    if context_window:
        _token_usage["context_window"] = context_window
    if compact_threshold:
        _token_usage["compact_threshold"] = compact_threshold


@tool(
    "context_window_status",
    (
        "Check the current context window usage. Returns token counts, "
        "percentage used, and whether compaction is recommended. "
        "Call this to monitor context usage during long conversations."
    ),
    {
        "type": "object",
        "properties": {},
    },
)
async def context_window_status() -> str:
    window = _token_usage.get("context_window", 0) or 200_000
    total = _token_usage.get("total_tokens", 0)
    input_t = _token_usage.get("input_tokens", 0)
    output_t = _token_usage.get("output_tokens", 0)
    compact_at = _token_usage.get("compact_threshold", 0) or int(window * 0.60)
    pct = round(total / window * 100, 1) if window else 0.0

    return json.dumps(
        {
            "ok": True,
            "input_tokens": input_t,
            "output_tokens": output_t,
            "total_tokens": total,
            "context_window": window,
            "compact_threshold": compact_at,
            "percent_used": pct,
            "should_compact": total > compact_at,
            "status": (
                "critical" if pct > 80 else "warning" if pct > 60 else "healthy"
            ),
        },
    )


@tool(
    "list_unix_capabilities",
    "Describe enabled Unix/system automation capabilities and active guardrails.",
    {
        "type": "object",
        "properties": {},
    },
)
async def list_unix_capabilities() -> str:
    tool_names = [spec.name for spec in get_system_tool_specs()]
    return json.dumps(
        {
            "ok": True,
            "unsafe_full_access": _unsafe_full_access_enabled(),
            "guardrails": {
                "allowed_commands": sorted(_read_allowed_commands()),
                "denied_commands": sorted(_read_denied_commands()),
                "base_dir": str(_resolve_base_dir()) if _resolve_base_dir() else "",
            },
            "tools_count": len(tool_names),
            "tools": tool_names,
        },
    )


# ---------------------------------------------------------------------------
# TODO list — lightweight task tracker for agent planning
# ---------------------------------------------------------------------------

_todo_items: list[dict[str, str]] = []


@tool(
    "todo_write",
    (
        "Create or update a task list to track progress. "
        "Accepts a JSON array of todo objects, each with 'content' (str), "
        "'status' ('pending'|'in_progress'|'completed'), and 'activeForm' (str, "
        "present-tense description shown while task runs). "
        "Replaces the full list each call. Returns the updated list."
    ),
    {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                        "activeForm": {"type": "string"},
                    },
                    "required": ["content", "status"],
                },
                "description": "The full todo list (replaces previous).",
            },
        },
        "required": ["todos"],
    },
)
async def todo_write(todos: Any = None) -> str:
    global _todo_items
    if todos is None:
        todos = []
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except (json.JSONDecodeError, ValueError):
            return json.dumps({"ok": False, "error": "todos must be a JSON array"})
    if not isinstance(todos, list):
        return json.dumps({"ok": False, "error": "todos must be a JSON array"})
    _todo_items = [
        {
            "content": str(t.get("content", "")),
            "status": str(t.get("status", "pending")),
            "activeForm": str(t.get("activeForm", "")),
        }
        for t in todos
        if isinstance(t, dict)
    ]
    return json.dumps({"ok": True, "count": len(_todo_items), "todos": _todo_items})


@tool(
    "report_intent",
    "Report the agent's current intent or plan before acting. Call this before starting any significant task to surface what you are about to do.",
    {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "The agent's current intent or high-level plan",
            },
        },
        "required": ["intent"],
    },
)
async def report_intent(intent: str) -> str:
    return json.dumps({"ok": True, "intent": intent})


# ---------------------------------------------------------------------------
# Plan-mode toggle — module-level callbacks set by the CLI layer.
# ---------------------------------------------------------------------------
_set_permission_mode_callback: Any = None
_plan_approval_callback: Any = None


def set_permission_mode_callback(cb: Any) -> None:
    """Register the CLI callback that changes the permission mode."""
    global _set_permission_mode_callback
    _set_permission_mode_callback = cb


def set_plan_approval_callback(cb: Any) -> None:
    """Register the renderer callback that gates plan-mode exit on user approval.

    The callback signature is::

        async def approve(plan_summary: str) -> bool

    It should present the plan to the user and return ``True`` if
    approved, ``False`` if denied.  When no callback is registered
    the mode switch happens immediately (backwards-compatible).
    """
    global _plan_approval_callback
    _plan_approval_callback = cb


@tool(
    "enter_plan_mode",
    "Switch to plan mode. In plan mode only read-only tools are allowed. "
    "Use this when you need to explore the codebase and design an "
    "implementation plan before making changes.",
    {
        "type": "object",
        "properties": {},
    },
)
async def enter_plan_mode() -> str:
    if _set_permission_mode_callback is not None:
        try:
            _set_permission_mode_callback("plan")
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})
    return json.dumps({"ok": True, "mode": "plan"})


@tool(
    "exit_plan_mode",
    "Exit plan mode and return to default permissions so that write and "
    "execute tools become available again.  Requires user approval via the "
    "renderer before the mode switch takes effect.",
    {
        "type": "object",
        "properties": {
            "plan_summary": {
                "type": "string",
                "description": "Short summary of the plan being approved.",
            },
        },
    },
)
async def exit_plan_mode(plan_summary: str = "") -> str:
    # If a renderer approval callback is registered, gate on it.
    if _plan_approval_callback is not None:
        try:
            approved = _plan_approval_callback(plan_summary)
            if asyncio.iscoroutine(approved) or asyncio.isfuture(approved):
                approved = await approved
            if not approved:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "Plan not approved by user. Staying in plan mode.",
                        "mode": "plan",
                    }
                )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    if _set_permission_mode_callback is not None:
        try:
            _set_permission_mode_callback("default")
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})
    return json.dumps({"ok": True, "mode": "default"})


@tool(
    "list_system_tools",
    "List available built-in system tools and their metadata.",
    {
        "type": "object",
        "properties": {},
    },
)
async def list_system_tools() -> str:
    tool_specs = get_system_tool_specs()
    data = [
        {
            "name": spec.name,
            "description": spec.description,
            "required_tier": spec.required_tier,
        }
        for spec in tool_specs
    ]
    return json.dumps({"ok": True, "count": len(data), "tools": data})


# ---------------------------------------------------------------------------
# ask_user — interactive choice/question tool
# ---------------------------------------------------------------------------

# Module-level callback set by the CLI layer.  When ``None`` the tool falls
# back to returning an error asking the model to rephrase as a text question.
_ask_user_callback: Any = None

# Flag set when ask_user fires during a turn so the CLI can skip auto-detection.
_ask_user_called: bool = False


def set_ask_user_callback(cb: Any) -> None:
    """Register the CLI callback for the ``ask_user`` tool."""
    global _ask_user_callback
    _ask_user_callback = cb


def was_ask_user_called() -> bool:
    """Return whether ``ask_user`` was invoked since the last reset."""
    return _ask_user_called


def reset_ask_user_called() -> None:
    """Reset the per-turn ``ask_user`` flag."""
    global _ask_user_called
    _ask_user_called = False


@tool(
    "ask_user",
    "Present the user with a question and a set of choices, and return "
    "their selection.  Use this when you need the user to pick between "
    "options or confirm a decision before proceeding.",
    {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to present to the user.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of choices the user can pick from. "
                "If empty, a free-text input is shown instead.",
            },
            "allow_custom": {
                "type": "boolean",
                "description": "If true, the user can type a custom response "
                "in addition to the listed choices. Defaults to false.",
            },
        },
        "required": ["question"],
    },
)
async def ask_user(
    question: str,
    choices: list[str] | None = None,
    allow_custom: bool = False,
) -> str:
    """Present choices to the user via the TUI widget and return the selection."""
    global _ask_user_called
    _ask_user_called = True

    if _ask_user_callback is None:
        return _json_error(
            "no_ui",
            detail="Interactive UI not available. "
            "Ask the user directly in your text response instead.",
        )

    try:
        result = await _ask_user_callback(
            question=question,
            choices=choices or [],
            allow_custom=allow_custom,
        )
        return json.dumps({"ok": True, "selected": result})
    except Exception as exc:
        return _json_error("ask_user_failed", detail=str(exc))


@tool(
    "user_ask",
    "Present the user with one or more structured questions.  Accepts the "
    "Claude Code AskUserQuestion format — an array of question objects each "
    "containing a question string, header, options with labels/descriptions, "
    "and a multiSelect flag.  Returns the user's answers.",
    {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "One or more questions to present to the user.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The full question text.",
                        },
                        "header": {
                            "type": "string",
                            "description": "Short label displayed as a chip/tag.",
                        },
                        "options": {
                            "type": "array",
                            "description": "Available choices.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Display text for the option.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Explanation of what this option means.",
                                    },
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "Allow multiple selections.",
                        },
                    },
                    "required": ["question"],
                },
            },
            "question": {
                "type": "string",
                "description": "Simple question text (flat format, alternative to questions array).",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Flat list of choices (used with question param).",
            },
        },
    },
)
async def user_ask(
    questions: list[dict[str, Any]] | None = None,
    question: str | None = None,
    choices: list[str] | None = None,
) -> str:
    """Handle Claude Code AskUserQuestion format by flattening into ask_user calls.

    Accepts either the structured ``questions`` array (Claude Code style) or a
    flat ``question`` string + optional ``choices`` list (Copilot / simple style).
    """
    global _ask_user_called
    _ask_user_called = True

    if _ask_user_callback is None:
        return _json_error(
            "no_ui",
            detail="Interactive UI not available. "
            "Ask the user directly in your text response instead.",
        )

    # Flat question fallback (Copilot or simple invocation)
    if not questions and question:
        questions = [{"question": question, "options": [{"label": c, "description": ""} for c in (choices or [])]}]

    if not questions:
        return _json_error("invalid_args", detail="No questions provided.")

    answers: dict[str, str] = {}
    for q_obj in questions:
        q_text = q_obj.get("question", "")
        if not q_text:
            continue
        header = q_obj.get("header", "")
        options = q_obj.get("options", [])
        multi = q_obj.get("multiSelect", False)

        # Build choice labels from structured options
        choice_labels: list[str] = []
        for opt in options:
            label = opt.get("label", "")
            desc = opt.get("description", "")
            if label and desc:
                choice_labels.append(f"{label} — {desc}")
            elif label:
                choice_labels.append(label)

        prompt = f"[{header}] {q_text}" if header else q_text

        try:
            result = await _ask_user_callback(
                question=prompt,
                choices=choice_labels,
                allow_custom=True,
                multi_select=multi,
            )
            answers[q_text] = result
        except TypeError:
            # Callback doesn't support multi_select — fall back
            try:
                result = await _ask_user_callback(
                    question=prompt,
                    choices=choice_labels,
                    allow_custom=True,
                )
                answers[q_text] = result
            except Exception as exc:
                answers[q_text] = f"error: {exc}"
        except Exception as exc:
            answers[q_text] = f"error: {exc}"

    return json.dumps({"ok": True, "answers": answers})


# ---------------------------------------------------------------------------
# user_interact — unified permission / notification / question tool
# ---------------------------------------------------------------------------

# Module-level callback set by the CLI layer.
_user_interact_callback: Any = None


def set_user_interact_callback(cb: Any) -> None:
    """Register the CLI callback for the ``user_interact`` tool."""
    global _user_interact_callback
    _user_interact_callback = cb


async def _handle_ui_permission(action: str, reason: str, risk: str) -> str:
    """Handle permission mode of user_interact."""
    if _user_interact_callback is None:
        return _json_error(
            "no_ui",
            detail="Interactive UI not available. "
            "Ask the user directly in your text response instead.",
        )
    try:
        result = await _user_interact_callback(
            mode="permission",
            action=action,
            reason=reason,
            risk=risk,
        )
        approved = result.get("approved", False)
        return json.dumps(
            {
                "ok": True,
                "approved": approved,
                "action": "approve" if approved else "deny",
            },
        )
    except Exception as exc:
        return _json_error("permission_failed", detail=str(exc))


async def _handle_ui_notify(
    title: str,
    message: str,
    priority: str,
    channels: list[str] | None,
) -> str:
    """Handle notify mode of user_interact."""
    resolved_channels = channels or ["tui", "bell"]
    delivered: list[str] = []

    # TUI channel — uses callback if available
    if "tui" in resolved_channels and _user_interact_callback is not None:
        try:
            await _user_interact_callback(
                mode="notify",
                title=title,
                message=message,
                priority=priority,
            )
            delivered.append("tui")
        except Exception:
            pass

    # OS notification channel — use NativeNotifier
    if "os" in resolved_channels:
        try:
            from obscura.agent.interaction import AttentionPriority
            from obscura.notifications.native import NativeNotifier

            prio_map = {
                "low": AttentionPriority.LOW,
                "normal": AttentionPriority.NORMAL,
                "high": AttentionPriority.HIGH,
                "critical": AttentionPriority.CRITICAL,
            }
            notifier = NativeNotifier()
            await notifier.notify(
                title,
                message,
                priority=prio_map.get(priority, AttentionPriority.NORMAL),
                sound=False,  # sound handled separately via "sound" channel
            )
            delivered.append("os")
        except Exception:
            pass

    # Terminal bell
    if "bell" in resolved_channels:
        sys.stdout.write("\a")
        sys.stdout.flush()
        delivered.append("bell")

    # Sound (macOS only via NativeNotifier)
    if "sound" in resolved_channels:
        try:
            if sys.platform == "darwin":
                import asyncio as _asyncio

                proc = await _asyncio.create_subprocess_exec(
                    "afplay",
                    "/System/Library/Sounds/Glass.aiff",
                    stdout=_asyncio.subprocess.DEVNULL,
                    stderr=_asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
                delivered.append("sound")
        except Exception:
            pass

    return json.dumps({"ok": True, "delivered": True, "channels": delivered})


async def _handle_ui_question(
    question: str,
    choices: list[str] | None,
    allow_custom: bool,
) -> str:
    """Handle question mode of user_interact."""
    if _user_interact_callback is None:
        return _json_error(
            "no_ui",
            detail="Interactive UI not available. "
            "Ask the user directly in your text response instead.",
        )
    try:
        result = await _user_interact_callback(
            mode="question",
            question=question,
            choices=choices or [],
            allow_custom=allow_custom,
        )
        return json.dumps({"ok": True, "selected": result.get("selected", "")})
    except Exception as exc:
        return _json_error("question_failed", detail=str(exc))


@tool(
    "user_interact",
    "Interact with the user. "
    "permission: action + reason + risk → approved true/false. "
    "notify: title + message + priority (no response). "
    "question: question + optional choices (free-text if no choices). "
    "multi_select: question + choices → list of selected items.",
    {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["permission", "notify", "question", "multi_select"],
                "description": "Interaction mode.",
            },
            "action": {
                "type": "string",
                "description": "(permission) The action being requested.",
            },
            "reason": {
                "type": "string",
                "description": "(permission) Why this action is needed.",
            },
            "risk": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
                "description": "(permission) Risk level affecting visual styling.",
            },
            "title": {
                "type": "string",
                "description": "(notify) Notification title.",
            },
            "message": {
                "type": "string",
                "description": "(notify/permission) Message body.",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "critical"],
                "description": "(notify) Priority level affecting delivery channels.",
            },
            "channels": {
                "type": "array",
                "items": {"type": "string", "enum": ["tui", "os", "bell", "sound"]},
                "description": "(notify) Delivery channels. Default: ['tui', 'bell'].",
            },
            "question": {
                "type": "string",
                "description": "(question) The question to present.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "(question) Optional list of choices.",
            },
            "allow_custom": {
                "type": "boolean",
                "description": "(question) Allow free-text response alongside choices.",
            },
        },
        "required": ["mode"],
    },
)
async def user_interact(
    mode: str,
    # permission params
    action: str = "",
    reason: str = "",
    risk: str = "low",
    # notify params
    title: str = "",
    message: str = "",
    priority: str = "normal",
    channels: list[str] | None = None,
    # question params
    question: str = "",
    choices: list[str] | None = None,
    allow_custom: bool = False,
) -> str:
    """Unified user interaction tool with permission, notify, and question modes."""
    if mode == "permission":
        return await _handle_ui_permission(action, reason, risk)
    if mode == "notify":
        return await _handle_ui_notify(title, message, priority, channels)
    if mode == "question":
        return await _handle_ui_question(question, choices, allow_custom)
    if mode == "multi_select":
        return await _handle_ui_multi_select(question, choices)
    return _json_error("invalid_mode", detail=f"Unknown mode: {mode}")


# ---------------------------------------------------------------------------
# Focused interaction tools — clean, single-purpose alternatives
# ---------------------------------------------------------------------------


async def _handle_ui_multi_select(
    question: str,
    choices: list[str] | None,
) -> str:
    """Handle multi_select mode of user_interact."""
    if _user_interact_callback is None:
        return _json_error(
            "no_ui",
            detail="Interactive UI not available.",
        )
    if not choices:
        return _json_error("no_choices", detail="Multi-select requires choices.")
    try:
        result = await _user_interact_callback(
            mode="multi_select",
            question=question,
            choices=choices,
        )
        return json.dumps({"ok": True, "selected": result.get("selected", [])})
    except Exception as exc:
        return _json_error("multi_select_failed", detail=str(exc))


# ---------------------------------------------------------------------------
# History snip — selective context compression
# ---------------------------------------------------------------------------


# Module-level message history reference (set by REPL).
_snip_message_history: list[Any] | None = None


def set_snip_message_history(history: list[Any]) -> None:
    """Set the message history reference for the snip tool."""
    global _snip_message_history
    _snip_message_history = history


@tool(
    "history_snip",
    (
        "Remove specific message segments from the conversation history "
        "to free context window space. Specify a range of turn indices to remove."
    ),
    {
        "type": "object",
        "properties": {
            "start_turn": {
                "type": "integer",
                "description": "First turn index to remove (0-based).",
            },
            "end_turn": {
                "type": "integer",
                "description": "Last turn index to remove (inclusive).",
            },
            "reason": {
                "type": "string",
                "description": "Why these turns are being removed.",
            },
        },
        "required": ["start_turn", "end_turn"],
    },
)
async def history_snip(
    start_turn: int,
    end_turn: int,
    reason: str = "",
) -> str:
    if _snip_message_history is None:
        return json.dumps(
            {
                "ok": False,
                "error": "no_history",
                "detail": "Message history not available",
            },
        )

    try:
        start_turn = int(start_turn)
    except (TypeError, ValueError):
        start_turn = 0
    try:
        end_turn = int(end_turn)
    except (TypeError, ValueError):
        end_turn = 0

    total = len(_snip_message_history)
    if start_turn < 0 or end_turn >= total or start_turn > end_turn:
        return json.dumps(
            {
                "ok": False,
                "error": "invalid_range",
                "detail": f"Range {start_turn}-{end_turn} invalid (history has {total} entries)",
            },
        )

    # Remove the specified range.
    removed_count = end_turn - start_turn + 1
    del _snip_message_history[start_turn : end_turn + 1]

    return json.dumps(
        {
            "ok": True,
            "removed_turns": removed_count,
            "remaining_turns": len(_snip_message_history),
            "reason": reason,
        },
    )


# ---------------------------------------------------------------------------
# Notebook edit — Jupyter notebook cell editing
# ---------------------------------------------------------------------------


@tool(
    "notebook_edit",
    (
        "Edit a Jupyter notebook (.ipynb) cell. Supports replacing cell content, "
        "inserting new cells, or deleting cells."
    ),
    {
        "type": "object",
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": "Path to the .ipynb file.",
            },
            "cell_index": {
                "type": "integer",
                "description": "0-based index of the cell to edit/insert after/delete.",
            },
            "new_source": {
                "type": "string",
                "description": "New cell source content (required for replace/insert).",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": "Cell type (required for insert).",
            },
            "edit_mode": {
                "type": "string",
                "enum": ["replace", "insert", "delete"],
                "description": "Edit mode: replace, insert after cell_index, or delete (default: replace).",
            },
        },
        "required": ["notebook_path", "cell_index"],
    },
)
async def notebook_edit(
    notebook_path: str,
    cell_index: int,
    new_source: str = "",
    cell_type: str = "code",
    edit_mode: str = "replace",
) -> str:
    target = _resolve_path(notebook_path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if target.suffix.lower() != ".ipynb":
        return _json_error("not_a_notebook", path=str(target))

    try:
        nb_data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _json_error("notebook_parse_error", detail=str(exc))

    cells = nb_data.get("cells", [])

    try:
        cell_index = int(cell_index)
    except (TypeError, ValueError):
        cell_index = 0

    if edit_mode == "delete":
        if cell_index < 0 or cell_index >= len(cells):
            return _json_error(
                "cell_index_out_of_range",
                index=cell_index,
                total_cells=len(cells),
            )
        old_source = "".join(cells[cell_index].get("source", []))
        del cells[cell_index]
        target.write_text(
            json.dumps(nb_data, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return json.dumps(
            {
                "ok": True,
                "edit_mode": "delete",
                "cell_index": cell_index,
                "deleted_source": old_source[:200],
                "cell_count": len(cells),
            },
        )

    if edit_mode == "replace":
        if cell_index < 0 or cell_index >= len(cells):
            return _json_error(
                "cell_index_out_of_range",
                index=cell_index,
                total_cells=len(cells),
            )
        old_source = "".join(cells[cell_index].get("source", []))
        cells[cell_index]["source"] = new_source.splitlines(keepends=True)
        if cell_type:
            cells[cell_index]["cell_type"] = cell_type
        target.write_text(
            json.dumps(nb_data, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return json.dumps(
            {
                "ok": True,
                "edit_mode": "replace",
                "cell_index": cell_index,
                "cell_type": cells[cell_index].get("cell_type", "code"),
                "old_source": old_source[:200],
                "new_source": new_source[:200],
            },
        )

    if edit_mode == "insert":
        if cell_index < -1 or cell_index >= len(cells):
            return _json_error(
                "cell_index_out_of_range",
                index=cell_index,
                total_cells=len(cells),
            )
        new_cell: dict[str, Any] = {
            "cell_type": cell_type,
            "source": new_source.splitlines(keepends=True),
            "metadata": {},
            "outputs": [] if cell_type == "code" else [],
        }
        if cell_type == "code":
            new_cell["execution_count"] = None
        cells.insert(cell_index + 1, new_cell)
        target.write_text(
            json.dumps(nb_data, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return json.dumps(
            {
                "ok": True,
                "edit_mode": "insert",
                "inserted_after": cell_index,
                "cell_type": cell_type,
                "new_source": new_source[:200],
                "cell_count": len(cells),
            },
        )

    return _json_error("invalid_edit_mode", detail=f"Unknown edit_mode: {edit_mode}")


# ---------------------------------------------------------------------------
# Tool search — deferred tool discovery
# ---------------------------------------------------------------------------

# Global reference set by the REPL/runtime at startup.
_tool_registry_ref: Any = None


def set_tool_registry(registry: Any) -> None:
    """Set the global ToolRegistry reference for tool_search."""
    global _tool_registry_ref
    _tool_registry_ref = registry


@tool(
    "tool_search",
    (
        "Search for available tools by name or keyword. "
        "Use 'select:ToolName' for exact match, or keywords for fuzzy search."
    ),
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query. 'select:name' for exact match, or keywords.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results (default 5).",
            },
        },
        "required": ["query"],
    },
)
async def tool_search(query: str, max_results: int = 5) -> str:
    if _tool_registry_ref is None:
        return _json_error("no_registry", detail="Tool registry not available")

    all_specs = _tool_registry_ref.all()
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 5
    cap = max(1, min(max_results, 50))

    # Exact match mode: "select:ToolName,OtherTool"
    if query.startswith("select:"):
        names = [n.strip() for n in query[7:].split(",") if n.strip()]
        found = []
        for name in names:
            spec = _tool_registry_ref.get(name)
            if spec is not None:
                found.append({"name": spec.name, "description": spec.description})
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "matches": found,
                "total_tools": len(all_specs),
            },
        )

    # Keyword search: score each tool by query term matches.
    terms = query.lower().split()
    scored: list[tuple[float, Any]] = []
    for spec in all_specs:
        name_lower = spec.name.lower()
        desc_lower = spec.description.lower()
        score = 0.0
        for term in terms:
            if term == name_lower:
                score += 10.0  # Exact name match
            elif term in name_lower:
                score += 5.0  # Partial name match
            if term in desc_lower:
                score += 1.0  # Description match
        if score > 0:
            scored.append((score, spec))

    scored.sort(key=lambda x: x[0], reverse=True)
    matches = [
        {"name": spec.name, "description": spec.description} for _, spec in scored[:cap]
    ]
    return json.dumps(
        {
            "ok": True,
            "query": query,
            "matches": matches,
            "total_tools": len(all_specs),
        },
    )


# ---------------------------------------------------------------------------
# Sleep tool — wait/poll for proactive-mode agents
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config tool — read/write settings from within agent
# ---------------------------------------------------------------------------


@tool(
    "config",
    "Read or write Obscura settings (~/.obscura/settings.json).",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set", "list"],
                "description": "Action: 'get' a key, 'set' a key, or 'list' all settings.",
            },
            "key": {
                "type": "string",
                "description": "Settings key (dot-notation, e.g. 'backend.default').",
            },
            "value": {
                "description": "Value to set (for 'set' action). Can be string, number, bool, or null.",
            },
        },
        "required": ["action"],
    },
)
async def config_tool(
    action: str,
    key: str = "",
    value: Any = None,
) -> str:
    settings_path = Path.home() / ".obscura" / "settings.json"

    # Load current settings.
    settings: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            settings = {}

    if action == "list":
        return json.dumps({"ok": True, "settings": settings})

    if action == "get":
        if not key:
            return _json_error(
                "missing_key",
                detail="'key' is required for 'get' action",
            )
        # Support dot-notation: "backend.default" → settings["backend"]["default"]
        parts = key.split(".")
        current: Any = settings
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return json.dumps(
                    {"ok": True, "key": key, "value": None, "found": False},
                )
        return json.dumps({"ok": True, "key": key, "value": current, "found": True})

    if action == "set":
        if not key:
            return _json_error(
                "missing_key",
                detail="'key' is required for 'set' action",
            )
        parts = key.split(".")
        target_dict = settings
        for part in parts[:-1]:
            if part not in target_dict or not isinstance(target_dict[part], dict):
                target_dict[part] = {}
            target_dict = target_dict[part]
        target_dict[parts[-1]] = value
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, indent=2) + "\n",
            encoding="utf-8",
        )
        return json.dumps({"ok": True, "key": key, "value": value, "written": True})

    return _json_error("invalid_action", detail=f"Unknown action: {action}")


def _merge_lines(old: list[str], new: list[str]) -> tuple[list[str], bool]:
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


@tool(
    "write_agent_shared",
    (
        "Write to the shared vault zone. Backs up the previous version and "
        "attempts a line-level fork-merge. Returns merged/conflict flags."
    ),
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to vault/shared/ (e.g. 'decisions/plan.md'). "
                    "Must not escape vault/shared/."
                ),
            },
            "text": {"type": "string", "description": "Content to write."},
        },
        "required": ["path", "text"],
    },
)
async def write_agent_shared(path: str, text: str) -> str:
    import datetime

    from obscura.core.paths import resolve_obscura_home

    shared_root = (resolve_obscura_home() / "vault" / "shared").resolve()

    # Resolve the target path and guard against traversal.
    candidate = (shared_root / path).resolve()
    try:
        candidate.relative_to(shared_root)
    except ValueError:
        return _json_error(
            "path_not_allowed",
            detail="Resolved path escapes vault/shared/",
            path=path,
        )

    backed_up = False
    merged = False
    had_conflict = False

    if candidate.exists():
        ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
        backup_dir = shared_root / ".backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{candidate.name}.{ts}.bak"
        try:
            old_bytes = candidate.read_bytes()
            backup_path.write_bytes(old_bytes)
            backed_up = True
        except OSError as exc:
            return _json_error(
                "backup_failed",
                path=str(candidate),
                detail=str(exc),
            )

        # Fork-merge: split both versions into lines and reconcile.
        old_lines = old_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
        new_lines = text.splitlines(keepends=True)
        merged_lines, had_conflict = _merge_lines(old_lines, new_lines)
        final_text = "".join(merged_lines)
        merged = True
    else:
        final_text = text

    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(final_text, encoding="utf-8")
    except OSError as exc:
        return _json_error("write_failed", path=str(candidate), detail=str(exc))

    return json.dumps(
        {
            "ok": True,
            "path": str(candidate),
            "backed_up": backed_up,
            "merged": merged,
            "conflict": had_conflict,
        }
    )


def get_system_tool_specs() -> list[ToolSpec]:
    """Return default system tool specs for agent runtime."""
    static_specs = [
        # Execution
        cast("ToolSpec", cast("Any", run_python3).spec),
        cast("ToolSpec", cast("Any", run_command).spec),
        cast("ToolSpec", cast("Any", run_shell).spec),
        # Web
        cast("ToolSpec", cast("Any", web_fetch).spec),
        cast("ToolSpec", cast("Any", web_search).spec),
        # Delegation
        cast("ToolSpec", cast("Any", task).spec),
        # System discovery
        cast("ToolSpec", cast("Any", which_command).spec),
        # Filesystem — basic
        cast("ToolSpec", cast("Any", list_directory).spec),
        cast("ToolSpec", cast("Any", read_text_file).spec),
        cast("ToolSpec", cast("Any", write_text_file).spec),
        cast("ToolSpec", cast("Any", append_text_file).spec),
        cast("ToolSpec", cast("Any", write_agent_shared).spec),
        cast("ToolSpec", cast("Any", make_directory).spec),
        cast("ToolSpec", cast("Any", remove_path).spec),
        # Filesystem — advanced
        cast("ToolSpec", cast("Any", grep_files).spec),
        cast("ToolSpec", cast("Any", find_files).spec),
        cast("ToolSpec", cast("Any", edit_text_file).spec),
        cast("ToolSpec", cast("Any", copy_path).spec),
        cast("ToolSpec", cast("Any", move_path).spec),
        cast("ToolSpec", cast("Any", file_info).spec),
        cast("ToolSpec", cast("Any", tree_directory).spec),
        cast("ToolSpec", cast("Any", diff_files).spec),
        # Git
        cast("ToolSpec", cast("Any", git).spec),
        # Utilities
        cast("ToolSpec", cast("Any", download_file).spec),
        cast("ToolSpec", cast("Any", http_request).spec),
        cast("ToolSpec", cast("Any", clipboard_read).spec),
        cast("ToolSpec", cast("Any", clipboard_write).spec),
        cast("ToolSpec", cast("Any", json_query).spec),
        # Context window
        cast("ToolSpec", cast("Any", context_window_status).spec),
        # Dynamic tools + sandbox
        cast("ToolSpec", cast("Any", create_tool).spec),
        cast("ToolSpec", cast("Any", call_dynamic_tool).spec),
        cast("ToolSpec", cast("Any", list_dynamic_tools).spec),
        cast("ToolSpec", cast("Any", code_sandbox).spec),
        # Copilot GPT-5 Mini
        # System info
        cast("ToolSpec", cast("Any", get_environment).spec),
        cast("ToolSpec", cast("Any", get_system_info).spec),
        cast("ToolSpec", cast("Any", list_processes).spec),
        cast("ToolSpec", cast("Any", signal_process).spec),
        cast("ToolSpec", cast("Any", list_listening_ports).spec),
        cast("ToolSpec", cast("Any", list_unix_capabilities).spec),
        cast("ToolSpec", cast("Any", list_system_tools).spec),
        # Task tracking
        cast("ToolSpec", cast("Any", todo_write).spec),
        # Agent intent reporting
        cast("ToolSpec", cast("Any", report_intent).spec),
        # User interaction
        cast("ToolSpec", cast("Any", ask_user).spec),
        cast("ToolSpec", cast("Any", user_ask).spec),
        cast("ToolSpec", cast("Any", user_interact).spec),
        # Intelligence tools (context_snapshot, causal_trace, policy_probe)
        cast("ToolSpec", cast("Any", context_snapshot).spec),
        cast("ToolSpec", cast("Any", causal_trace).spec),
        cast("ToolSpec", cast("Any", policy_probe).spec),
        # Team prompt
        # History snip
        cast("ToolSpec", cast("Any", history_snip).spec),
        # Notebook edit
        cast("ToolSpec", cast("Any", notebook_edit).spec),
        # Tool search
        cast("ToolSpec", cast("Any", tool_search).spec),
        # Sleep & Config
        cast("ToolSpec", cast("Any", config_tool).spec),
    ]
    # Append any dynamically created tools
    for spec in _dynamic_tools.values():
        static_specs.append(spec)
    return static_specs

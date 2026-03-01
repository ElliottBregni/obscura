"""System command tools exposed to agent loops."""

from __future__ import annotations

import asyncio
import base64
import difflib
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

def _strip_html(raw: str) -> str:
    """Strip HTML tags and decode entities, returning plain text."""
    # Drop script/style blocks entirely
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
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
    mapping = cast(dict[Any, Any], value)
    return {str(key): item for key, item in mapping.items()}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _unsafe_full_access_enabled() -> bool:
    return _env_flag("OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS", default=False)


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
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
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
    required_tier="privileged",
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
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _json_error("timeout")
    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
    )


@tool(
    "run_npx",
    "Execute an npx command and return stdout/stderr/exit_code.",
    {
        "type": "object",
        "properties": {
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"},
        },
        "required": ["args"],
    },
    required_tier="privileged",
)
async def run_npx(
    args: list[str],
    cwd: str = "",
    timeout_seconds: float = 120.0,
) -> str:
    command = _resolve_command("npx")
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=(cwd or None),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _json_error("timeout")
    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
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
    required_tier="privileged",
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
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _json_error("timeout")

    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "command": normalized_command,
        }
    )


@tool(
    "run_shell",
    "Execute a shell command via /bin/zsh -lc and return stdout/stderr/exit_code.",
    {
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "Shell script to execute."},
            "command": {"type": "string", "description": "Alias for script (LLM compat)."},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"},
        },
    },
    required_tier="privileged",
)
async def run_shell(
    script: str = "",
    command: str = "",
    cwd: str = "",
    timeout_seconds: float = 60.0,
) -> str:
    actual_script = script or command
    if not actual_script:
        return json.dumps({"ok": False, "error": "no_script_provided"})
    return await run_command(
        "/bin/zsh",
        args=["-lc", actual_script],
        cwd=cwd,
        timeout_seconds=float(timeout_seconds),
    )


@tool(
    "web_fetch",
    (
        "Fetch a URL and return the page content as plain text. "
        "Provide a `prompt` describing what to extract (e.g. 'list the top 5 stock gainers') "
        "and the response will include that context alongside the body so you can extract it. "
        "HTML is automatically stripped to clean readable text."
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
    required_tier="privileged",
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
    timeout_seconds = float(timeout_seconds)
    max_bytes = int(max_bytes)
    request_headers = headers or {}
    payload = body.encode("utf-8") if body else None
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
            response_headers = {k: v for k, v in response.headers.items()}
            content_type = response_headers.get("Content-Type", "").lower()
            is_html = "html" in content_type or text.lstrip().startswith("<")
            body_text = _strip_html(text) if is_html else text
            result: dict[str, object] = {
                "ok": True,
                "url": url,
                "final_url": response.geturl(),
                "status": getattr(response, "status", 200),
                "content_type": content_type,
                "body": body_text,
                "truncated": truncated,
                "bytes_read": len(data),
            }
            if prompt:
                result["prompt"] = prompt
            return json.dumps(result)
    except url_error.HTTPError as exc:
        raw_error = exc.read(max_bytes)
        return json.dumps(
            {
                "ok": False,
                "url": url,
                "status": exc.code,
                "error": "http_error",
                "body": raw_error.decode("utf-8", errors="replace"),
            }
        )
    except Exception as exc:
        return _json_error("web_fetch_failed", url=url, detail=str(exc))


@tool(
    "run_python",
    "Execute Python code and return stdout/stderr/exit_code. Alias for run_python3.",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"},
        },
        "required": ["code"],
    },
    required_tier="privileged",
)
async def run_python(
    code: str,
    cwd: str = "",
    timeout_seconds: float = 30.0,
) -> str:
    return await run_python3(code, cwd=cwd, timeout_seconds=timeout_seconds)


@tool(
    "web_search",
    "Search the web for a query and return concise result items.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    },
    required_tier="privileged",
)
async def web_search(query: str, max_results: int = 5) -> str:
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

    clean = lambda s: re.sub(r"<[^>]+>", "", _html.unescape(s)).strip()

    titles   = [clean(t) for t in re.findall(r'class="result__a"[^>]*>(.*?)</a>', raw_html)]
    snippets = [clean(s) for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</span>', raw_html, re.DOTALL)]
    hrefs    = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', raw_html)
    urls_fb  = [clean(u) for u in re.findall(r'class="result__url"[^>]*>\s*(.*?)\s*</a>', raw_html, re.DOTALL)]

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
        items.append({"title": title, "url": url, "snippet": snippet})

    return json.dumps({"ok": True, "query": query, "count": len(items), "results": items})


@tool(
    "task",
    "Compatibility tool for agent task delegation. Returns a structured status.",
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "target": {"type": "string"},
        },
        "required": ["prompt"],
    },
    required_tier="privileged",
)
async def task(prompt: str, target: str = "") -> str:
    return json.dumps(
        {
            "ok": False,
            "error": "task_delegation_not_configured",
            "prompt": prompt,
            "target": target,
            "message": "Configure A2A remote tools to enable real delegation.",
        }
    )


@tool(
    "which_command",
    "Resolve an executable path for a command name.",
    {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
    required_tier="privileged",
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
        }
    )


@tool(
    "discover_all_commands",
    "Discover available shell commands on the host with optional prefix filtering.",
    {
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "prefix": {"type": "string"},
            "include_builtins": {"type": "boolean"},
        },
    },
    required_tier="privileged",
)
async def discover_all_commands(
    limit: int = 500,
    prefix: str = "",
    include_builtins: bool = True,
) -> str:
    safe_limit = max(1, min(limit, 5000))
    # Prefer bash compgen (portable), then fall back to shelling out to `which -a`.
    compgen_type = "-c" if include_builtins else "-A command"
    payload = json.loads(
        await run_command(
            "bash",
            args=["-lc", f"compgen {compgen_type} | sort -u"],
            timeout_seconds=30.0,
        )
    )
    if not payload.get("ok", False):
        fallback = json.loads(
            await run_shell(
                "echo \"$PATH\" | tr ':' '\\n' | while read -r p; do ls -1 \"$p\" 2>/dev/null; done | sort -u",
                timeout_seconds=30.0,
            )
        )
        if not fallback.get("ok", False):
            return json.dumps(payload)
        payload = fallback

    stdout = str(payload.get("stdout", ""))
    commands = [line.strip() for line in stdout.splitlines() if line.strip()]
    if prefix:
        commands = [cmd for cmd in commands if cmd.startswith(prefix)]
    commands = commands[:safe_limit]
    return json.dumps(
        {
            "ok": True,
            "count": len(commands),
            "limit": safe_limit,
            "prefix": prefix,
            "include_builtins": include_builtins,
            "commands": commands,
        }
    )


@tool(
    "list_directory",
    "List files/directories at a path.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    required_tier="privileged",
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
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
                "is_file": child.is_file(),
                "size": child.stat().st_size if child.is_file() else 0,
            }
        )
    return json.dumps({"ok": True, "path": str(target), "entries": entries})


@tool(
    "read_text_file",
    "Read a UTF-8 text file.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}, "max_bytes": {"type": "integer"}},
        "required": ["path"],
    },
    required_tier="privileged",
)
async def read_text_file(path: str, max_bytes: int = 200_000) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_file():
        return _json_error("not_a_file", path=str(target))

    data = target.read_bytes()
    truncated = False
    if len(data) > max_bytes:
        data = data[:max_bytes]
        truncated = True
    text = data.decode("utf-8", errors="replace")
    return json.dumps(
        {
            "ok": True,
            "path": str(target),
            "text": text,
            "truncated": truncated,
            "bytes_read": len(data),
        }
    )


@tool(
    "write_text_file",
    "Write UTF-8 text to a file (overwrites by default).",
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
    required_tier="privileged",
)
async def write_text_file(
    path: str,
    text: str,
    overwrite: bool = True,
    create_dirs: bool = True,
) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if target.exists() and target.is_dir():
        return _json_error("path_is_directory", path=str(target))
    if target.exists() and not overwrite:
        return _json_error("file_exists", path=str(target))
    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return json.dumps(
        {
            "ok": True,
            "path": str(target),
            "bytes_written": len(text.encode("utf-8")),
        }
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
    required_tier="privileged",
)
async def append_text_file(path: str, text: str, create_dirs: bool = True) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if target.exists() and target.is_dir():
        return _json_error("path_is_directory", path=str(target))
    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(text)
    return json.dumps(
        {
            "ok": True,
            "path": str(target),
            "bytes_appended": len(text.encode("utf-8")),
        }
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
    required_tier="privileged",
)
async def make_directory(
    path: str,
    parents: bool = True,
    exist_ok: bool = True,
) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    target.mkdir(parents=parents, exist_ok=exist_ok)
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
    required_tier="privileged",
)
async def remove_path(
    path: str,
    recursive: bool = False,
    missing_ok: bool = True,
) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
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
    required_tier="privileged",
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
    required_tier="privileged",
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
    required_tier="privileged",
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
    required_tier="privileged",
)
async def signal_process(pid: int, signal: str = "TERM") -> str:
    return await run_command(
        "kill", args=[f"-{signal}", str(pid)], timeout_seconds=10.0
    )


@tool(
    "list_listening_ports",
    "List listening TCP/UDP ports.",
    {
        "type": "object",
        "properties": {},
    },
    required_tier="privileged",
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


@tool(
    "security_lookup",
    "Run common Unix security lookups (world_writable, suid, listening_ports, logged_in_users, failed_logins).",
    {
        "type": "object",
        "properties": {
            "check": {"type": "string"},
            "path": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["check"],
    },
    required_tier="privileged",
)
async def security_lookup(
    check: Literal[
        "world_writable",
        "suid",
        "listening_ports",
        "logged_in_users",
        "failed_logins",
    ],
    path: str = "/",
    max_results: int = 100,
) -> str:
    limited = max(1, min(max_results, 500))
    if check == "listening_ports":
        return await list_listening_ports()

    if check == "logged_in_users":
        return await run_command("who", timeout_seconds=20.0)

    if check == "failed_logins":
        if shutil.which("lastb"):
            return await run_command("lastb", timeout_seconds=20.0)
        if platform.system() == "Darwin":
            return await run_command(
                "log",
                args=[
                    "show",
                    "--last",
                    "1d",
                    "--predicate",
                    'eventMessage CONTAINS[c] "failed"',
                ],
                timeout_seconds=20.0,
            )
        return _json_error("failed_logins_unsupported")

    if check == "world_writable":
        target = _resolve_path(path)
        if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
            return _json_error("path_not_allowed", path=str(target))
        return await run_shell(
            f"find {str(target)!r} -xdev -type f -perm -0002 2>/dev/null | head -n {limited}",
            timeout_seconds=60.0,
        )

    # suid
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    return await run_shell(
        f"find {str(target)!r} -xdev -type f -perm -4000 2>/dev/null | head -n {limited}",
        timeout_seconds=60.0,
    )


@tool(
    "manage_crontab",
    "Manage user cron automation entries (list, add, remove).",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "schedule": {"type": "string"},
            "command": {"type": "string"},
            "marker": {"type": "string"},
        },
        "required": ["action"],
    },
    required_tier="privileged",
)
async def manage_crontab(
    action: Literal["list", "add", "remove"],
    schedule: str = "",
    command: str = "",
    marker: str = "obscura",
) -> str:
    if shutil.which("crontab") is None:
        return _json_error("crontab_not_found")

    if action == "list":
        current = await run_command("crontab", args=["-l"], timeout_seconds=20.0)
        payload = json.loads(current)
        if payload.get("ok"):
            lines = str(payload.get("stdout", "")).splitlines()
            filtered = [line for line in lines if marker in line]
            payload["filtered_entries"] = filtered
            payload["filtered_count"] = len(filtered)
            return json.dumps(payload)
        # Accept empty crontab as non-fatal
        stderr = str(payload.get("stderr", ""))
        if "no crontab for" in stderr.lower():
            return json.dumps({"ok": True, "entries": [], "filtered_entries": []})
        return current

    if action == "add":
        if not schedule.strip() or not command.strip():
            return _json_error("schedule_and_command_required")
        list_payload = json.loads(
            await run_command("crontab", args=["-l"], timeout_seconds=20.0)
        )
        existing = ""
        if list_payload.get("ok"):
            existing = str(list_payload.get("stdout", ""))
        entry = f"{schedule} {command} # {marker}".rstrip()
        new_content = existing.rstrip("\n")
        new_content = f"{new_content}\n{entry}\n" if new_content else f"{entry}\n"
        return await run_shell(
            f"cat <<'EOF' | crontab -\n{new_content}EOF",
            timeout_seconds=20.0,
        )

    # remove
    list_payload = json.loads(
        await run_command("crontab", args=["-l"], timeout_seconds=20.0)
    )
    existing_lines: list[str] = []
    if list_payload.get("ok"):
        existing_lines = str(list_payload.get("stdout", "")).splitlines()
    else:
        stderr = str(list_payload.get("stderr", ""))
        if "no crontab for" not in stderr.lower():
            return json.dumps(list_payload)
    kept = [line for line in existing_lines if marker not in line]
    new_content = "\n".join(kept).rstrip("\n")
    return await run_shell(
        f"cat <<'EOF' | crontab -\n{new_content}\nEOF",
        timeout_seconds=20.0,
    )


@tool(
    "list_unix_capabilities",
    "Describe enabled Unix/system automation capabilities and active guardrails.",
    {
        "type": "object",
        "properties": {},
    },
    required_tier="privileged",
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
        }
    )


@tool(
    "list_system_tools",
    "List available built-in system tools and their metadata.",
    {
        "type": "object",
        "properties": {},
    },
    required_tier="privileged",
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


def get_system_tool_specs() -> list[ToolSpec]:
    """Return default system tool specs for agent runtime."""
    return [
        cast(ToolSpec, getattr(cast(Any, run_python3), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_python), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_npx), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_command), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_shell), "spec")),
        cast(ToolSpec, getattr(cast(Any, web_fetch), "spec")),
        cast(ToolSpec, getattr(cast(Any, web_search), "spec")),
        cast(ToolSpec, getattr(cast(Any, task), "spec")),
        cast(ToolSpec, getattr(cast(Any, which_command), "spec")),
        cast(ToolSpec, getattr(cast(Any, discover_all_commands), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_directory), "spec")),
        cast(ToolSpec, getattr(cast(Any, read_text_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, write_text_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, append_text_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, make_directory), "spec")),
        cast(ToolSpec, getattr(cast(Any, remove_path), "spec")),
        cast(ToolSpec, getattr(cast(Any, get_environment), "spec")),
        cast(ToolSpec, getattr(cast(Any, get_system_info), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_processes), "spec")),
        cast(ToolSpec, getattr(cast(Any, signal_process), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_listening_ports), "spec")),
        cast(ToolSpec, getattr(cast(Any, security_lookup), "spec")),
        cast(ToolSpec, getattr(cast(Any, manage_crontab), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_unix_capabilities), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_system_tools), "spec")),
    ]

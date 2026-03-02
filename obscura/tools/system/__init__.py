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
    (
        "Delegate a sub-task to a local Obscura agent subprocess. "
        "Spawns 'obscura -p <prompt>' and returns the captured output. "
        "Use 'target' to specify an agent_type hint (e.g. 'explore', 'bash'); "
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
    required_tier="privileged",
)
async def task(prompt: str, target: str = "", timeout_seconds: float = 120.0) -> str:
    obscura_bin = _resolve_command("obscura")
    cmd = [obscura_bin, "-p", prompt]
    if target:
        cmd += ["--agent-type", target]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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
            return json.dumps({"ok": False, "error": "timeout", "prompt": prompt})
        output = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        return json.dumps({
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "result": output,
            "stderr": err,
            "prompt": prompt,
            "target": target,
        })
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "error": "delegation_failed",
            "message": str(exc),
            "prompt": prompt,
            "target": target,
        })


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


# ---------------------------------------------------------------------------
# File operation tools
# ---------------------------------------------------------------------------


@tool(
    "grep_files",
    "Search file contents with regex. Returns matching lines with file paths and line numbers.",
    {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for."},
            "path": {"type": "string", "description": "File or directory to search in."},
            "include": {"type": "string", "description": "Glob filter for filenames (e.g. '*.py')."},
            "max_results": {"type": "integer"},
            "case_sensitive": {"type": "boolean"},
        },
        "required": ["pattern", "path"],
    },
    required_tier="privileged",
)
async def grep_files(
    pattern: str,
    path: str,
    include: str = "",
    max_results: int = 100,
    case_sensitive: bool = True,
) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return _json_error("invalid_regex", pattern=pattern, detail=str(exc))

    limit = max(1, min(max_results, 1000))
    matches: list[dict[str, object]] = []

    def _search_file(fp: Path) -> None:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            return
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(matches) >= limit:
                return
            if regex.search(line):
                matches.append({
                    "file": str(fp),
                    "line": lineno,
                    "text": line.rstrip()[:500],
                })

    if target.is_file():
        _search_file(target)
    else:
        for fp in sorted(target.rglob("*")):
            if len(matches) >= limit:
                break
            if not fp.is_file():
                continue
            if include and not fnmatch.fnmatch(fp.name, include):
                continue
            # Skip binary-looking files
            if fp.suffix in {".pyc", ".pyo", ".so", ".dylib", ".bin", ".exe", ".o", ".a", ".class", ".jar", ".whl", ".gz", ".zip", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"}:
                continue
            _search_file(fp)

    return json.dumps({
        "ok": True,
        "pattern": pattern,
        "path": str(target),
        "count": len(matches),
        "truncated": len(matches) >= limit,
        "matches": matches,
    })


@tool(
    "find_files",
    "Find files by glob pattern or name. Returns matching file paths with metadata.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to search in."},
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py', '**/*.ts')."},
            "name": {"type": "string", "description": "Exact or partial filename to match."},
            "max_results": {"type": "integer"},
            "file_type": {"type": "string", "description": "'file', 'dir', or 'any'."},
        },
        "required": ["path"],
    },
    required_tier="privileged",
)
async def find_files(
    path: str,
    pattern: str = "**/*",
    name: str = "",
    max_results: int = 200,
    file_type: str = "any",
) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_dir():
        return _json_error("not_a_directory", path=str(target))

    limit = max(1, min(max_results, 2000))
    results: list[dict[str, object]] = []

    for fp in sorted(target.glob(pattern)):
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
            results.append({
                "path": str(fp),
                "name": fp.name,
                "is_dir": fp.is_dir(),
                "size": st.st_size if fp.is_file() else 0,
            })
        except OSError:
            results.append({"path": str(fp), "name": fp.name, "is_dir": fp.is_dir(), "size": 0})

    return json.dumps({
        "ok": True,
        "path": str(target),
        "pattern": pattern,
        "count": len(results),
        "truncated": len(results) >= limit,
        "results": results,
    })


@tool(
    "edit_text_file",
    "Perform a surgical find-and-replace edit in a file. Replaces the first (or all) occurrence(s) of old_text with new_text.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "description": "Text to find (exact match)."},
            "new_text": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default: false)."},
        },
        "required": ["path", "old_text", "new_text"],
    },
    required_tier="privileged",
)
async def edit_text_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> str:
    target = _resolve_path(path)
    if not _unsafe_full_access_enabled() and not _is_path_allowed(target):
        return _json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return _json_error("path_not_found", path=str(target))
    if not target.is_file():
        return _json_error("not_a_file", path=str(target))

    content = target.read_text(encoding="utf-8")
    if old_text not in content:
        return _json_error("text_not_found", path=str(target), old_text=old_text[:200])

    if replace_all:
        new_content = content.replace(old_text, new_text)
        count = content.count(old_text)
    else:
        new_content = content.replace(old_text, new_text, 1)
        count = 1

    target.write_text(new_content, encoding="utf-8")
    return json.dumps({
        "ok": True,
        "path": str(target),
        "replacements": count,
        "bytes_written": len(new_content.encode("utf-8")),
    })


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
    required_tier="privileged",
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
    required_tier="privileged",
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
    required_tier="privileged",
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
        ".py": "text/x-python", ".js": "text/javascript", ".ts": "text/typescript",
        ".json": "application/json", ".yaml": "text/yaml", ".yml": "text/yaml",
        ".md": "text/markdown", ".txt": "text/plain", ".html": "text/html",
        ".css": "text/css", ".sh": "text/x-shellscript", ".toml": "text/toml",
        ".xml": "text/xml", ".csv": "text/csv", ".sql": "text/x-sql",
        ".rs": "text/x-rust", ".go": "text/x-go", ".java": "text/x-java",
        ".c": "text/x-c", ".cpp": "text/x-c++", ".h": "text/x-c",
        ".rb": "text/x-ruby", ".php": "text/x-php", ".swift": "text/x-swift",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".svg": "image/svg+xml", ".pdf": "application/pdf",
        ".zip": "application/zip", ".gz": "application/gzip",
    }
    return ext_map.get(path.suffix.lower(), "application/octet-stream")


@tool(
    "tree_directory",
    "Show a recursive directory tree with optional depth limit and file filters.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_depth": {"type": "integer", "description": "Max recursion depth (default 3)."},
            "include": {"type": "string", "description": "Glob filter for filenames."},
            "show_hidden": {"type": "boolean"},
            "max_entries": {"type": "integer"},
        },
        "required": ["path"],
    },
    required_tier="privileged",
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

    depth = max(1, min(max_depth, 10))
    limit = max(1, min(max_entries, 5000))
    lines: list[str] = [str(target)]
    count = 0

    def _walk(dir_path: Path, prefix: str, current_depth: int) -> None:
        nonlocal count
        if current_depth > depth or count >= limit:
            return
        try:
            children = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
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
    return json.dumps({
        "ok": True,
        "path": str(target),
        "entries": count,
        "truncated": count >= limit,
        "tree": "\n".join(lines),
    })


@tool(
    "diff_files",
    "Compare two files and return a unified diff.",
    {
        "type": "object",
        "properties": {
            "file_a": {"type": "string"},
            "file_b": {"type": "string"},
            "context_lines": {"type": "integer", "description": "Lines of context (default 3)."},
        },
        "required": ["file_a", "file_b"],
    },
    required_tier="privileged",
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
        lines_a = path_a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lines_b = path_b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError as exc:
        return _json_error("read_failed", detail=str(exc))

    ctx = max(0, min(context_lines, 20))
    diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=str(path_a), tofile=str(path_b), n=ctx))
    diff_text = "".join(diff)
    return json.dumps({
        "ok": True,
        "file_a": str(path_a),
        "file_b": str(path_b),
        "identical": len(diff) == 0,
        "diff": diff_text[:100_000],
    })


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
        git_cmd, *args,
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "timeout"}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


@tool(
    "git_status",
    "Show the working tree status (staged, unstaged, untracked files).",
    {
        "type": "object",
        "properties": {
            "cwd": {"type": "string", "description": "Repository path (default: current dir)."},
            "short": {"type": "boolean", "description": "Use short format (default: true)."},
        },
    },
    required_tier="privileged",
)
async def git_status(cwd: str = "", short: bool = True) -> str:
    args = ["status"]
    if short:
        args.append("--short")
    args.append("--branch")
    result = await _git(args, cwd=cwd)
    return json.dumps(result)


@tool(
    "git_diff",
    "Show changes between commits, working tree, or staging area.",
    {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Ref to diff against (e.g. 'HEAD', 'main', commit hash)."},
            "staged": {"type": "boolean", "description": "Show staged changes (--cached)."},
            "path": {"type": "string", "description": "Limit diff to a specific file/dir."},
            "stat_only": {"type": "boolean", "description": "Show diffstat only (--stat)."},
            "cwd": {"type": "string"},
        },
    },
    required_tier="privileged",
)
async def git_diff(
    ref: str = "",
    staged: bool = False,
    path: str = "",
    stat_only: bool = False,
    cwd: str = "",
) -> str:
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
    # Truncate large diffs
    if result.get("ok") and len(result.get("stdout", "")) > 100_000:
        result["stdout"] = result["stdout"][:100_000] + "\n... (truncated)"
        result["truncated"] = True
    return json.dumps(result)


@tool(
    "git_log",
    "Show commit history with optional filters.",
    {
        "type": "object",
        "properties": {
            "max_count": {"type": "integer", "description": "Number of commits (default 10)."},
            "oneline": {"type": "boolean", "description": "One-line format (default true)."},
            "path": {"type": "string", "description": "Limit to commits touching this path."},
            "author": {"type": "string", "description": "Filter by author."},
            "since": {"type": "string", "description": "Show commits after date (e.g. '2024-01-01')."},
            "ref": {"type": "string", "description": "Branch or ref to show (default: current)."},
            "cwd": {"type": "string"},
        },
    },
    required_tier="privileged",
)
async def git_log(
    max_count: int = 10,
    oneline: bool = True,
    path: str = "",
    author: str = "",
    since: str = "",
    ref: str = "",
    cwd: str = "",
) -> str:
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
    result = await _git(args, cwd=cwd)
    return json.dumps(result)


@tool(
    "git_commit",
    "Stage files and create a git commit.",
    {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message."},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to stage. Use ['.'] for all changes.",
            },
            "cwd": {"type": "string"},
        },
        "required": ["message"],
    },
    required_tier="privileged",
)
async def git_commit(
    message: str,
    files: list[str] | None = None,
    cwd: str = "",
) -> str:
    if not message.strip():
        return _json_error("empty_commit_message")

    # Stage files
    stage_files = files or ["."]
    add_result = await _git(["add"] + stage_files, cwd=cwd)
    if not add_result.get("ok"):
        return json.dumps(add_result)

    # Commit
    result = await _git(["commit", "-m", message], cwd=cwd)
    return json.dumps(result)


@tool(
    "git_branch",
    "List, create, or switch git branches.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "'list', 'create', or 'switch'."},
            "name": {"type": "string", "description": "Branch name (for create/switch)."},
            "cwd": {"type": "string"},
        },
        "required": ["action"],
    },
    required_tier="privileged",
)
async def git_branch(
    action: str = "list",
    name: str = "",
    cwd: str = "",
) -> str:
    if action == "list":
        result = await _git(["branch", "-a", "--no-color"], cwd=cwd)
        return json.dumps(result)
    elif action == "create":
        if not name.strip():
            return _json_error("branch_name_required")
        result = await _git(["checkout", "-b", name], cwd=cwd)
        return json.dumps(result)
    elif action == "switch":
        if not name.strip():
            return _json_error("branch_name_required")
        result = await _git(["checkout", name], cwd=cwd)
        return json.dumps(result)
    else:
        return _json_error("invalid_action", action=action, valid=["list", "create", "switch"])


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
            "max_bytes": {"type": "integer", "description": "Max download size in bytes (default 50MB)."},
        },
        "required": ["url", "path"],
    },
    required_tier="privileged",
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
    req = url_request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; Obscura/1.0)",
    })

    try:
        with url_request.urlopen(req, timeout=timeout_seconds) as resp:
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return _json_error("file_too_large", url=url, max_bytes=max_bytes)
            target.write_bytes(data[:max_bytes])
            return json.dumps({
                "ok": True,
                "url": url,
                "path": str(target),
                "bytes_written": len(data),
                "content_type": resp.headers.get("Content-Type", ""),
            })
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
            "json_body": {"type": "object", "description": "JSON body (auto-sets Content-Type)."},
            "timeout_seconds": {"type": "number"},
        },
        "required": ["url"],
    },
    required_tier="privileged",
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

    req = url_request.Request(
        url=url,
        method=method.upper(),
        headers=req_headers,
        data=payload,
    )

    try:
        with url_request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read(500_000)
            text = raw.decode("utf-8", errors="replace")
            response_headers = {k: v for k, v in resp.headers.items()}
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
            }

            # Try to parse JSON response
            if "json" in content_type.lower():
                try:
                    result["json"] = json.loads(text)
                except json.JSONDecodeError:
                    pass

            return json.dumps(result)
    except url_error.HTTPError as exc:
        raw_error = exc.read(100_000)
        return json.dumps({
            "ok": False,
            "status": exc.code,
            "url": url,
            "method": method.upper(),
            "error": "http_error",
            "body": raw_error.decode("utf-8", errors="replace"),
            "headers": {k: v for k, v in exc.headers.items()} if exc.headers else {},
        })
    except Exception as exc:
        return _json_error("request_failed", url=url, method=method.upper(), detail=str(exc))


@tool(
    "clipboard_read",
    "Read the current system clipboard contents (macOS only).",
    {
        "type": "object",
        "properties": {},
    },
    required_tier="privileged",
)
async def clipboard_read() -> str:
    if platform.system() != "Darwin":
        return _json_error("clipboard_unsupported", platform=platform.system())
    result = await run_command("pbpaste", timeout_seconds=5.0)
    payload = json.loads(result)
    if payload.get("ok"):
        return json.dumps({
            "ok": True,
            "text": payload.get("stdout", ""),
            "bytes": len(payload.get("stdout", "").encode("utf-8")),
        })
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
    required_tier="privileged",
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
            proc.communicate(input=text.encode("utf-8")), timeout=5.0
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _json_error("timeout")
    if proc.returncode != 0:
        return _json_error("clipboard_write_failed", stderr=stderr.decode("utf-8", errors="replace"))
    return json.dumps({
        "ok": True,
        "bytes_written": len(text.encode("utf-8")),
    })


@tool(
    "json_query",
    "Query a JSON file or string using dot-notation paths (e.g. 'data.users[0].name').",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to a JSON file (optional if data provided)."},
            "data": {"type": "string", "description": "Raw JSON string to query."},
            "query": {"type": "string", "description": "Dot-notation path (e.g. 'users[0].name', 'config.database.host')."},
            "keys_only": {"type": "boolean", "description": "Return only keys at the query path."},
        },
        "required": ["query"],
    },
    required_tier="privileged",
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
            if isinstance(part, int):
                current = current[part]
            elif isinstance(current, dict):
                current = current[part]
            elif isinstance(current, list):
                current = current[int(part)]
            else:
                return _json_error("invalid_path", query=query, at=part)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            return _json_error("path_not_found_in_data", query=query, at=part, detail=str(exc))

    if keys_only and isinstance(current, dict):
        return json.dumps({"ok": True, "query": query, "keys": list(current.keys())})

    # Serialize the result
    try:
        result_str = json.dumps(current)
    except (TypeError, ValueError):
        result_str = str(current)

    return json.dumps({"ok": True, "query": query, "result": current if isinstance(current, (str, int, float, bool, type(None), list, dict)) else result_str})


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
            "name": {"type": "string", "description": "Tool name (lowercase, underscored)."},
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
                    "asyncio, subprocess, urllib. Example: 'return json.dumps({\"ok\": True, \"result\": kwargs[\"x\"] * 2})'"
                ),
            },
        },
        "required": ["name", "description", "code"],
    },
    required_tier="privileged",
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
    sandbox_globals: dict[str, Any] = {
        "__builtins__": __builtins__,
        "json": json,
        "os": os,
        "re": re,
        "Path": Path,
        "asyncio": asyncio,
        "subprocess": subprocess,
        "platform": platform,
        "shutil": shutil,
        "url_request": url_request,
        "url_parse": url_parse,
        "url_error": url_error,
        "base64": base64,
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
        required_tier="privileged",
    )
    _dynamic_tools[clean_name] = spec

    return json.dumps({
        "ok": True,
        "name": clean_name,
        "description": description,
        "message": f"Tool '{clean_name}' created. Call it with the tool name '{clean_name}'.",
    })


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
    required_tier="privileged",
)
async def call_dynamic_tool(name: str, args: dict[str, Any] | None = None) -> str:
    clean_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
    spec = _dynamic_tools.get(clean_name)
    if spec is None:
        available = list(_dynamic_tools.keys())
        return _json_error("dynamic_tool_not_found", name=clean_name, available=available)

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
    required_tier="privileged",
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
            "cwd": {"type": "string", "description": "Working directory for execution."},
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
    required_tier="privileged",
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
        if lang in ("python", "python3"):
            args = [str(save_path)]
        elif lang in ("node", "nodejs", "javascript", "js"):
            args = [str(save_path)]
        elif lang in ("bash", "zsh", "sh", "shell"):
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
            proc.communicate(input=input_data), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return json.dumps({
            "ok": False,
            "error": "timeout",
            "language": lang,
            "timeout_seconds": timeout_seconds,
        })

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


@tool(
    "copilot_query",
    (
        "Send a prompt to GitHub Copilot using GPT-5 Mini. "
        "Runs 'copilot --model gpt-5-mini -p <prompt>' in single-shot mode "
        "and returns the response. Use this to get a second opinion, "
        "cross-check answers, generate alternative solutions, or delegate "
        "subtasks to a fast lightweight model."
    ),
    {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The prompt to send to Copilot GPT-5 Mini.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Max seconds to wait (default 60, max 300).",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the copilot process.",
            },
        },
        "required": ["prompt"],
    },
    required_tier="privileged",
)
async def copilot_query(
    prompt: str,
    timeout_seconds: float = 60.0,
    cwd: str = "",
) -> str:
    """Run a single-shot copilot query with gpt-5-mini."""
    timeout_seconds = max(5.0, min(float(timeout_seconds), 300.0))

    cmd = _resolve_command("copilot")
    if not cmd:
        return _json_error("command_not_found", command="copilot")

    proc = await asyncio.create_subprocess_exec(
        cmd,
        "--model", "gpt-5-mini",
        "-p", prompt,
        cwd=(cwd or None),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ),
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return json.dumps({
            "ok": False,
            "error": "timeout",
            "timeout_seconds": timeout_seconds,
        })

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")

    return json.dumps({
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "model": "gpt-5-mini",
        "response": stdout_text[:100_000],
        "stderr": stderr_text[:10_000] if stderr_text.strip() else "",
    })


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
    required_tier="privileged",
)
async def context_window_status() -> str:
    window = _token_usage.get("context_window", 0) or 200_000
    total = _token_usage.get("total_tokens", 0)
    input_t = _token_usage.get("input_tokens", 0)
    output_t = _token_usage.get("output_tokens", 0)
    compact_at = _token_usage.get("compact_threshold", 0) or int(window * 0.60)
    pct = round(total / window * 100, 1) if window else 0.0

    return json.dumps({
        "ok": True,
        "input_tokens": input_t,
        "output_tokens": output_t,
        "total_tokens": total,
        "context_window": window,
        "compact_threshold": compact_at,
        "percent_used": pct,
        "should_compact": total > compact_at,
        "status": (
            "critical" if pct > 80
            else "warning" if pct > 60
            else "healthy"
        ),
    })


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


# ---------------------------------------------------------------------------
# Todo list — lightweight task tracker for agent planning
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
            }
        },
        "required": ["intent"],
    },
)
async def report_intent(intent: str) -> str:
    return json.dumps({"ok": True, "intent": intent})


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
    static_specs = [
        # Execution
        cast(ToolSpec, getattr(cast(Any, run_python3), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_python), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_npx), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_command), "spec")),
        cast(ToolSpec, getattr(cast(Any, run_shell), "spec")),
        # Web
        cast(ToolSpec, getattr(cast(Any, web_fetch), "spec")),
        cast(ToolSpec, getattr(cast(Any, web_search), "spec")),
        # Delegation
        cast(ToolSpec, getattr(cast(Any, task), "spec")),
        # System discovery
        cast(ToolSpec, getattr(cast(Any, which_command), "spec")),
        cast(ToolSpec, getattr(cast(Any, discover_all_commands), "spec")),
        # Filesystem — basic
        cast(ToolSpec, getattr(cast(Any, list_directory), "spec")),
        cast(ToolSpec, getattr(cast(Any, read_text_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, write_text_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, append_text_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, make_directory), "spec")),
        cast(ToolSpec, getattr(cast(Any, remove_path), "spec")),
        # Filesystem — advanced
        cast(ToolSpec, getattr(cast(Any, grep_files), "spec")),
        cast(ToolSpec, getattr(cast(Any, find_files), "spec")),
        cast(ToolSpec, getattr(cast(Any, edit_text_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, copy_path), "spec")),
        cast(ToolSpec, getattr(cast(Any, move_path), "spec")),
        cast(ToolSpec, getattr(cast(Any, file_info), "spec")),
        cast(ToolSpec, getattr(cast(Any, tree_directory), "spec")),
        cast(ToolSpec, getattr(cast(Any, diff_files), "spec")),
        # Git
        cast(ToolSpec, getattr(cast(Any, git_status), "spec")),
        cast(ToolSpec, getattr(cast(Any, git_diff), "spec")),
        cast(ToolSpec, getattr(cast(Any, git_log), "spec")),
        cast(ToolSpec, getattr(cast(Any, git_commit), "spec")),
        cast(ToolSpec, getattr(cast(Any, git_branch), "spec")),
        # Utilities
        cast(ToolSpec, getattr(cast(Any, download_file), "spec")),
        cast(ToolSpec, getattr(cast(Any, http_request), "spec")),
        cast(ToolSpec, getattr(cast(Any, clipboard_read), "spec")),
        cast(ToolSpec, getattr(cast(Any, clipboard_write), "spec")),
        cast(ToolSpec, getattr(cast(Any, json_query), "spec")),
        # Context window
        cast(ToolSpec, getattr(cast(Any, context_window_status), "spec")),
        # Dynamic tools + sandbox
        cast(ToolSpec, getattr(cast(Any, create_tool), "spec")),
        cast(ToolSpec, getattr(cast(Any, call_dynamic_tool), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_dynamic_tools), "spec")),
        cast(ToolSpec, getattr(cast(Any, code_sandbox), "spec")),
        # Copilot GPT-5 Mini
        cast(ToolSpec, getattr(cast(Any, copilot_query), "spec")),
        # System info
        cast(ToolSpec, getattr(cast(Any, get_environment), "spec")),
        cast(ToolSpec, getattr(cast(Any, get_system_info), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_processes), "spec")),
        cast(ToolSpec, getattr(cast(Any, signal_process), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_listening_ports), "spec")),
        cast(ToolSpec, getattr(cast(Any, security_lookup), "spec")),
        cast(ToolSpec, getattr(cast(Any, manage_crontab), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_unix_capabilities), "spec")),
        cast(ToolSpec, getattr(cast(Any, list_system_tools), "spec")),
        # Task tracking
        cast(ToolSpec, getattr(cast(Any, todo_write), "spec")),
        # Agent intent reporting
        cast(ToolSpec, getattr(cast(Any, report_intent), "spec")),
    ]
    # Append any dynamically created tools
    for spec in _dynamic_tools.values():
        static_specs.append(spec)
    return static_specs

"""Deterministic tool-result checks for the eval hook system.

Each checker inspects a TOOL_RESULT event and returns an error string
when something is wrong, or ``None`` when the result looks fine.  The
hook appends any error text to the event's ``tool_result`` so the LLM
sees the feedback and can self-correct on the next turn.

Checks:
- Python files: ruff lint, pyright type check, syntax parse, import validation
- Config files: YAML/TOML/JSON parse validation
- Shell scripts: bash -n syntax check
- Dockerfiles: hadolint (if available)
- All files: existence verification after write
- All tools: empty result detection
- Bash tool: hidden error/traceback detection
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
from typing import Any, cast

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Python checks
# ---------------------------------------------------------------------------


def check_python_syntax(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Parse Python file with ast to catch syntax errors (instant, no deps)."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not path.endswith(".py") or not os.path.isfile(path):
        return None
    try:
        source = open(path, encoding="utf-8").read()
        ast.parse(source, filename=path)
    except SyntaxError as exc:
        return f"\n⚠ Python syntax error: {exc.msg} (line {exc.lineno})"
    return None


def check_python_ruff(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Run ``ruff check`` on a Python file after write/edit."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not path.endswith(".py") or not os.path.isfile(path):
        return None
    try:
        proc = subprocess.run(
            ["ruff", "check", "--no-fix", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0 and proc.stdout.strip():
            return f"\n⚠ ruff check errors:\n{proc.stdout.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def check_python_pyright(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Run ``pyright`` on a Python file for type errors.

    Only runs if pyright is installed and the file is in a project
    with a pyrightconfig.json (to avoid slow global checks).
    """
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not path.endswith(".py") or not os.path.isfile(path):
        return None
    # Only run if pyrightconfig.json exists (project-level type checking)
    # Walk up to find it
    check_dir = os.path.dirname(os.path.abspath(path))
    found_config = False
    for _ in range(10):
        if os.path.isfile(os.path.join(check_dir, "pyrightconfig.json")):
            found_config = True
            break
        parent = os.path.dirname(check_dir)
        if parent == check_dir:
            break
        check_dir = parent
    if not found_config:
        return None

    try:
        proc = subprocess.run(
            ["pyright", "--outputjson", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0 and proc.stdout.strip():
            try:
                data = cast(dict[str, Any], json.loads(proc.stdout))
                diagnostics = cast(
                    list[dict[str, Any]], data.get("generalDiagnostics", [])
                )
                errors = [d for d in diagnostics if d.get("severity") == "error"]
                if errors:
                    msgs: list[str] = []
                    for e in errors[:5]:  # cap at 5
                        rng = cast(dict[str, Any], e.get("range", {}))
                        start = cast(dict[str, Any], rng.get("start", {}))
                        line = start.get("line", "?")
                        msgs.append(f"  line {line}: {e.get('message', '?')}")
                    return "\n⚠ pyright type errors:\n" + "\n".join(msgs)
            except json.JSONDecodeError:
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def check_python_imports(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Verify that top-level imports in a Python file can be resolved."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not path.endswith(".py") or not os.path.isfile(path):
        return None
    try:
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return None  # handled by check_python_syntax

    bad_imports: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if not _can_import(top):
                    bad_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top = node.module.split(".")[0]
                if not _can_import(top):
                    bad_imports.append(node.module)

    if bad_imports:
        return "\n⚠ Unresolvable imports: " + ", ".join(bad_imports[:5])
    return None


def _can_import(module_name: str) -> bool:
    """Check if a top-level module is importable (without actually importing)."""
    import importlib.util

    try:
        spec = importlib.util.find_spec(module_name)
        return spec is not None
    except (ModuleNotFoundError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Config file checks
# ---------------------------------------------------------------------------


def check_written_yaml_toml(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Validate YAML/TOML files after write."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not os.path.isfile(path):
        return None
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore[import-untyped]

            with open(path) as f:
                yaml.safe_load(f)
        except Exception as exc:
            return f"\n⚠ Invalid YAML: {exc}"
    elif path.endswith(".toml"):
        try:
            import tomllib

            with open(path, "rb") as f:
                tomllib.load(f)
        except Exception as exc:
            return f"\n⚠ Invalid TOML: {exc}"
    return None


def check_written_json(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Validate JSON files after write."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not path.endswith(".json") or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            json.load(f)
    except Exception as exc:
        return f"\n⚠ Invalid JSON: {exc}"
    return None


# ---------------------------------------------------------------------------
# Shell / Dockerfile checks
# ---------------------------------------------------------------------------


def check_shell_syntax(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Run ``bash -n`` to check shell script syntax."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not os.path.isfile(path):
        return None
    if not path.endswith((".sh", ".bash")):
        # Also check files that start with #!/bin/bash or #!/bin/sh
        try:
            with open(path) as f:
                first_line = f.readline()
            if not first_line.startswith("#!") or "sh" not in first_line:
                return None
        except Exception:
            return None
    try:
        proc = subprocess.run(
            ["bash", "-n", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0 and proc.stderr.strip():
            return f"\n⚠ Shell syntax error:\n{proc.stderr.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def check_dockerfile(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Run ``hadolint`` on Dockerfiles (if available)."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path or not os.path.isfile(path):
        return None
    basename = os.path.basename(path)
    if not (basename == "Dockerfile" or basename.startswith("Dockerfile.")):
        return None
    try:
        proc = subprocess.run(
            ["hadolint", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0 and proc.stdout.strip():
            lines = proc.stdout.strip().splitlines()[:5]
            return "\n⚠ hadolint warnings:\n" + "\n".join(lines)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ---------------------------------------------------------------------------
# Generic checks
# ---------------------------------------------------------------------------


def check_written_file_exists(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Verify the file was actually written to disk."""
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not path:
        return None
    if not os.path.exists(str(path)):
        return f"\n⚠ File was not created: {path}"
    return None


def _coerce_result_str(tool_result: str | dict[str, Any] | Any) -> str:
    """Coerce a tool result to string for eval checks.

    MCP tools may return dicts; system tools return JSON strings.
    """
    if isinstance(tool_result, str):
        return tool_result
    if isinstance(tool_result, dict):
        return json.dumps(tool_result)
    return str(tool_result) if tool_result is not None else ""


def check_empty_tool_result(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str | Any,
) -> str | None:
    """Flag tool results that are completely empty (likely an error)."""
    result_str = _coerce_result_str(tool_result)
    if not result_str or not result_str.strip():
        return "\n⚠ Tool returned empty result — may indicate a silent failure"
    return None


def check_bash_error(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str | Any,
) -> str | None:
    """Flag bash commands that produced error output."""
    lower = _coerce_result_str(tool_result).lower()
    if ("traceback" in lower or "error:" in lower) and "exit code 0" in lower:
        return "\n⚠ Command succeeded but produced error output — check for warnings"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chain_checks(*checkers: Any) -> Any:
    """Run multiple checkers, accumulate all errors (not just first)."""

    def _combined(
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: str,
    ) -> str | None:
        errors: list[str] = []
        for checker in checkers:
            result = checker(tool_name, tool_input, tool_result)
            if result is not None:
                errors.append(result)
        return "".join(errors) if errors else None

    return _combined


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

# Full Python check chain: syntax → ruff → pyright → imports
_python_full_check = _chain_checks(
    check_python_syntax,
    check_python_ruff,
    check_python_pyright,
    check_python_imports,
)

# File write check: exists + type-specific validation
_write_check = _chain_checks(
    check_written_file_exists,
    _python_full_check,
    check_written_yaml_toml,
    check_written_json,
    check_shell_syntax,
    check_dockerfile,
)

# Maps tool names to checker functions.
TOOL_CHECKS: dict[str, Any] = {
    # File write/edit tools
    "write_file": _write_check,
    "create_file": _write_check,
    "Write": _write_check,
    "edit_file": _python_full_check,
    "Edit": _python_full_check,
    # Bash tool
    "bash": check_bash_error,
    "Bash": check_bash_error,
    "run_command": check_bash_error,
}

# Generic checks that run on ALL tool results regardless of tool name
GENERIC_CHECKS: list[Any] = [
    check_empty_tool_result,
]


def run_tool_check(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> str | None:
    """Look up and run checks for *tool_name*.

    Runs tool-specific checks first, then generic checks.
    Returns error text to append, or ``None``.
    """
    errors: list[str] = []

    # Tool-specific check
    checker = TOOL_CHECKS.get(tool_name)
    if checker is not None:
        try:
            result = checker(tool_name, tool_input, tool_result)
            if result:
                errors.append(result)
        except Exception as exc:
            logger.debug("Tool eval check failed for %s: %s", tool_name, exc)

    # Generic checks (all tools)
    for generic in GENERIC_CHECKS:
        try:
            result = generic(tool_name, tool_input, tool_result)
            if result:
                errors.append(result)
        except Exception as exc:
            logger.debug("Generic eval check failed for %s: %s", tool_name, exc)

    return "".join(errors) if errors else None

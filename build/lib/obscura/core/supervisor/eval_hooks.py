"""Supervisor eval hooks — turn-level and session-level quality gates.

Layer 2: ``make_turn_eval_hook`` — fires on ``POST_MODEL_TURN``.
    Runs deterministic checks on tool results from the turn.  When checks
    fail, appends diagnostic context so the model can self-correct.

Layer 3: ``make_session_eval_gate`` — fires on ``PRE_MEMORY_COMMIT``.
    Runs a comprehensive eval before memory is persisted.  When the eval
    fails, returns ``False`` to block the memory commit and marks the
    session as failed.

Both hooks store results in the :class:`EvalResultStore` for regression
tracking when a store instance is provided.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

from obscura.core.supervisor.types import SupervisorHookPoint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer 2: Turn-level eval (POST_MODEL_TURN)
# ---------------------------------------------------------------------------


def _snapshot_dirty_files() -> set[str]:
    """Return the set of modified + untracked files in the working tree."""
    files: set[str] = set()
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        files.update(f for f in r.stdout.strip().splitlines() if f)
    except Exception:
        logger.debug("suppressed exception in _snapshot_dirty_files", exc_info=True)
    try:
        u = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        files.update(f for f in u.stdout.strip().splitlines() if f)
    except Exception:
        logger.debug("suppressed exception in _snapshot_dirty_files", exc_info=True)
    return files


def _revert_files(new_files: set[str]) -> list[str]:
    """Revert a set of files.  Returns list of successfully reverted paths."""
    import os

    reverted: list[str] = []
    for f in sorted(new_files):
        try:
            cr = subprocess.run(
                ["git", "checkout", "HEAD", "--", f],
                capture_output=True,
                timeout=5,
            )
            if cr.returncode != 0:
                os.remove(f)
            reverted.append(f)
        except Exception:
            logger.debug("suppressed exception in _revert_files", exc_info=True)
            continue
    return reverted


def _check_python_files(files: set[str]) -> dict[str, str]:
    """Run ruff on Python files in *files*.  Returns {path: diagnostics}."""
    errors: dict[str, str] = {}
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return errors
    try:
        proc = subprocess.run(
            ["ruff", "check", "--no-fix", *py_files],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0 and proc.stdout.strip():
            for line in proc.stdout.strip().splitlines():
                # ruff output: "path:line:col: CODE message"
                parts = line.split(":", 1)
                if parts:
                    path = parts[0].strip()
                    errors.setdefault(path, "")
                    errors[path] += line + "\n"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.debug("suppressed exception in _check_python_files", exc_info=True)
    return errors


async def turn_eval_handler(context: dict[str, Any]) -> dict[str, Any]:
    """POST_MODEL_TURN hook handler.

    Checks Python files changed during this turn for lint errors.
    Returns context with ``eval_errors`` key if issues found.
    """
    pre_files: set[str] = context.get("_eval_pre_files", set())
    post_files = _snapshot_dirty_files()
    new_files = post_files - pre_files

    if not new_files:
        return context

    errors = _check_python_files(new_files)
    if errors:
        summary = "\n".join(
            f"  {path}: {diag.strip()}" for path, diag in errors.items()
        )
        logger.warning("Turn eval found lint errors in %d file(s)", len(errors))
        context["eval_errors"] = errors
        context["eval_error_summary"] = summary

        # Optionally revert broken files
        if context.get("eval_revert_on_fail", False):
            reverted = _revert_files(set(errors.keys()))
            context["eval_reverted"] = reverted
            logger.info("Turn eval reverted %d file(s)", len(reverted))

    return context


def make_turn_eval_hook() -> tuple[SupervisorHookPoint, str, Any]:
    """Create a POST_MODEL_TURN hook entry tuple for SessionHookManager.

    Returns ``(hook_point, handler_ref, handler)`` ready for
    ``SessionHookManager.register()``.
    """
    return (
        SupervisorHookPoint.POST_MODEL_TURN,
        "eval:turn_eval",
        turn_eval_handler,
    )


# ---------------------------------------------------------------------------
# Layer 3: Session eval gate (PRE_MEMORY_COMMIT)
# ---------------------------------------------------------------------------


async def session_eval_gate_handler(context: dict[str, Any]) -> bool:
    """PRE_MEMORY_COMMIT hook handler.

    Runs a multi-signal eval before memory is persisted:
    1. Lint check — ruff on all dirty Python files
    2. Syntax check — ast.parse on all dirty Python files
    3. Unresolved tool errors — tool results still containing ⚠ markers
    4. Config validation — YAML/TOML/JSON parse check on dirty config files
    5. Empty session check — block if no meaningful output was produced

    Returns ``True`` to allow commit, ``False`` to block.
    """
    session_events = context.get("events", [])
    session_output = context.get("output_text", "")

    issues: list[str] = []
    score = 1.0

    # --- Signal 1: Collect unresolved tool errors ---
    tool_errors: list[str] = []
    for event in session_events:
        kind = getattr(event, "kind", None)
        if kind is not None and kind.value == "tool_result":
            result_text = getattr(event, "tool_result", "") or ""
            if "⚠" in result_text:
                tool_errors.append(
                    f"{getattr(event, 'tool_name', '?')}: {result_text[:200]}",
                )

    if tool_errors:
        issues.append(f"{len(tool_errors)} unresolved tool error(s)")
        score -= 0.2 * min(len(tool_errors), 3)

    # --- Signal 2: Lint check on dirty files ---
    dirty = _snapshot_dirty_files()
    lint_errors = _check_python_files(dirty)
    if lint_errors:
        error_count = sum(
            len(diag.strip().splitlines()) for diag in lint_errors.values()
        )
        issues.append(f"{error_count} lint error(s) in {len(lint_errors)} file(s)")
        score -= 0.3

    # --- Signal 3: Syntax check on dirty Python files ---
    import ast as _ast

    syntax_errors: list[str] = []
    for f in dirty:
        if not f.endswith(".py") or not os.path.isfile(f):
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                source = fp.read()
            _ast.parse(source, filename=f)
        except SyntaxError as exc:
            logger.debug(
                "suppressed exception in session_eval_gate_handler", exc_info=True
            )
            syntax_errors.append(f"{f}:{exc.lineno}: {exc.msg}")
    if syntax_errors:
        issues.append(f"{len(syntax_errors)} Python syntax error(s)")
        score -= 0.4  # syntax errors are severe

    # --- Signal 4: Config file validation ---
    config_errors: list[str] = []
    for f in dirty:
        if not os.path.isfile(f):
            continue
        if f.endswith((".yaml", ".yml")):
            try:
                import yaml

                with open(f) as fh:
                    yaml.safe_load(fh)
            except Exception as exc:
                logger.debug(
                    "suppressed exception in session_eval_gate_handler", exc_info=True
                )
                config_errors.append(f"{f}: invalid YAML ({exc})")
        elif f.endswith(".toml"):
            try:
                import tomllib

                with open(f, "rb") as fh:
                    tomllib.load(fh)
            except Exception as exc:
                logger.debug(
                    "suppressed exception in session_eval_gate_handler", exc_info=True
                )
                config_errors.append(f"{f}: invalid TOML ({exc})")
        elif f.endswith(".json"):
            try:
                import json as _json

                with open(f) as fh:
                    _json.load(fh)
            except Exception as exc:
                logger.debug(
                    "suppressed exception in session_eval_gate_handler", exc_info=True
                )
                config_errors.append(f"{f}: invalid JSON ({exc})")
    if config_errors:
        issues.append(f"{len(config_errors)} config parse error(s)")
        score -= 0.2

    # --- Signal 5: Empty session check ---
    has_output = bool(session_output and session_output.strip())
    has_tool_calls = any(
        getattr(e, "kind", None) is not None and e.kind.value == "tool_call"
        for e in session_events
    )
    if not has_output and not has_tool_calls:
        issues.append("Session produced no output and no tool calls")
        score -= 0.3

    # --- Decision ---
    score = max(score, 0.0)
    context["eval_score"] = score
    context["eval_issues"] = issues

    # Block if score below threshold (syntax errors alone trigger block)
    threshold = float(context.get("eval_threshold", 0.5))
    if score < threshold or syntax_errors:
        detail = "; ".join(issues) if issues else "below threshold"
        logger.warning(
            "Session eval gate: blocking (score=%.2f, threshold=%.2f) — %s",
            score,
            threshold,
            detail,
        )
        context["eval_blocked"] = True
        context["eval_lint_errors"] = lint_errors

        _try_persist_result(context, passed=False, score=score, detail=detail)

        # Record in eval memory for future recall
        try:
            from obscura.eval.memory import EvalMemory

            em = EvalMemory.get_instance()
            em.record_session_failure(
                session_id=context.get("session_id", "unknown"),
                reason=detail,
                lint_errors=lint_errors,
                tool_errors=tool_errors,
            )
        except Exception:
            logger.debug(
                "suppressed exception in session_eval_gate_handler", exc_info=True
            )

        return False

    # All clear
    _try_persist_result(context, passed=True, score=score, detail="clean")
    return True


def _try_persist_result(
    context: dict[str, Any],
    *,
    passed: bool,
    score: float,
    detail: str,
) -> None:
    """Best-effort persistence of eval result to EvalResultStore."""
    try:
        from obscura.eval.models import EvalRunSummary
        from obscura.eval.store import EvalResultStore

        session_id = context.get("session_id", "unknown")
        summary = EvalRunSummary(
            run_id=f"session-{session_id}-{int(time.time())}",
            suite_id="session:eval_gate",
            backend=str(context.get("backend", "unknown")),
            model=str(context.get("model", "unknown")),
            total_cases=1,
            passed=1 if passed else 0,
            failed=0 if passed else 1,
            regressions=0,
            errors=0,
            avg_deterministic_score=score,
            avg_judge_score=None,
            avg_composite_score=score,
        )
        store = EvalResultStore()
        import asyncio

        asyncio.create_task(store.save_run(summary))
    except Exception:
        logger.debug("suppressed exception in _try_persist_result", exc_info=True)


def make_session_eval_gate() -> tuple[SupervisorHookPoint, str, Any]:
    """Create a PRE_MEMORY_COMMIT hook entry tuple.

    Returns ``(hook_point, handler_ref, handler)`` ready for
    ``SessionHookManager.register()``.
    """
    return (
        SupervisorHookPoint.PRE_MEMORY_COMMIT,
        "eval:session_gate",
        session_eval_gate_handler,
    )

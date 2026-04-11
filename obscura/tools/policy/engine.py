"""obscura.tools.policy.engine — Policy evaluation logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obscura.tools.policy.models import PolicyResult, ToolPolicy

# Tools that operate on file-system paths.
_FS_TOOLS: frozenset[str] = frozenset(
    {
        "read_text_file",
        "write_text_file",
        "edit_text_file",
        "find_files",
        "grep_files",
        "append_text_file",
        "copy_path",
        "move_path",
        "remove_path",
        "list_directory",
        "tree_directory",
    },
)


def evaluate_policy(
    policy: ToolPolicy,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> PolicyResult:
    """Evaluate *policy* for a single tool invocation.

    Parameters
    ----------
    policy:
        The policy to evaluate.
    tool_name:
        Name of the tool being invoked.
    args:
        Tool arguments (used for base_dir path checking).

    Returns
    -------
    PolicyResult
        Whether the invocation is allowed and why.

    """
    if policy.full_access:
        return PolicyResult(
            allowed=True,
            reason="full_access granted",
            matched_rule="full_access",
        )

    if tool_name in policy.deny_list:
        return PolicyResult(
            allowed=False,
            reason=f"tool '{tool_name}' is in deny_list",
            matched_rule="deny_list",
        )

    if policy.allow_list and tool_name not in policy.allow_list:
        return PolicyResult(
            allowed=False,
            reason=f"tool '{tool_name}' is not in allow_list",
            matched_rule="allow_list",
        )

    # Action-level restrictions
    tool_args = args or {}
    action = tool_args.get("action")
    if action is not None:
        if tool_name in policy.denied_actions:
            if action in policy.denied_actions[tool_name]:
                return PolicyResult(
                    allowed=False,
                    reason=f"action '{action}' on '{tool_name}' is in denied_actions",
                    matched_rule="denied_actions",
                )
        if tool_name in policy.allowed_actions:
            if action not in policy.allowed_actions[tool_name]:
                return PolicyResult(
                    allowed=False,
                    reason=f"action '{action}' on '{tool_name}' is not in allowed_actions",
                    matched_rule="allowed_actions",
                )

    if policy.base_dir is not None and tool_name in _FS_TOOLS:
        result = _check_base_dir(policy.base_dir, args or {})
        if not result.allowed:
            return result

    return PolicyResult(
        allowed=True,
        reason="policy permits invocation",
        matched_rule="",
    )


def _check_base_dir(base_dir: Path, args: dict[str, Any]) -> PolicyResult:
    """Verify that any path argument stays within *base_dir*."""
    for key in ("path", "file_path", "directory"):
        raw = args.get(key)
        if raw is None:
            continue
        target = Path(str(raw)).resolve()
        try:
            target.relative_to(base_dir.resolve())
        except ValueError:
            return PolicyResult(
                allowed=False,
                reason=f"path '{target}' escapes base_dir '{base_dir}'",
                matched_rule="base_dir",
            )
    return PolicyResult(
        allowed=True,
        reason="path within base_dir",
        matched_rule="base_dir",
    )

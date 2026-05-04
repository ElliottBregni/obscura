"""obscura.core.permission_modes — Permission mode engine.

Implements named permission modes that control how tool execution
is gated (prompted, auto-approved, or blocked).

Modes:
  - DEFAULT: Prompt for each tool execution
  - PLAN: Read-only tools only (exploration mode)
  - ACCEPT_EDITS: Auto-approve file modification tools
  - BYPASS: Skip all permission checks (dangerous)

Also includes dangerous command pattern detection to block
destructive operations regardless of permission mode.

Action-level policy:
  Unified tools (like ``git``) expose multiple operations via an ``action``
  parameter.  ``READ_ONLY_ACTION_RULES`` restricts which actions are
  permitted in read-only contexts (PLAN mode, DIFF mode).  A tool that
  appears in this map is allowed **only** with the listed actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from obscura.core.enums.auth import PermissionMode


# Tools allowed in PLAN mode (read-only operations).
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_text_file",
        "grep_files",
        "find_files",
        "list_directory",
        "tree_directory",
        "file_info",
        "diff_files",
        "git",
        # Legacy per-action git tool names — predate the unified `git` tool.
        # Kept allowed so older agents and prompts that still call e.g.
        # `git_status` (rather than `git` with action="status") work in plan
        # mode. Write actions (commit/push/etc.) are still blocked because
        # they don't match this exact allowlist.
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "git_show",
        "web_search",
        "web_fetch",
        "which_command",
        "get_system_info",
        "get_environment",
        "context_window_status",
        "list_system_tools",
        "list_unix_capabilities",
        "tool_search",
        "json_query",
        "clipboard_read",
        "todo_write",
        "report_intent",
        "ask_user",
        "enter_plan_mode",
        "exit_plan_mode",
    },
)

# Action-level restrictions for read-only contexts.
# Tools in this map are only allowed with the listed actions when the
# permission mode restricts to read-only operations.
READ_ONLY_ACTION_RULES: dict[str, frozenset[str]] = {
    "git": frozenset({"status", "diff", "log"}),
}

# Tools auto-approved in ACCEPT_EDITS mode.
FILE_MODIFICATION_TOOLS: frozenset[str] = frozenset(
    {
        "write_text_file",
        "edit_text_file",
        "append_text_file",
        "make_directory",
        "remove_path",
        "copy_path",
        "move_path",
        "notebook_edit",
    },
)

# Dangerous command patterns — always denied regardless of mode.
_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"rm\s+-rf\s+/",
        r"rm\s+-rf\s+\*",
        r"git\s+push\s+--force\s+.*main",
        r"git\s+push\s+--force\s+.*master",
        r"git\s+reset\s+--hard",
        r"sudo\s+rm\s+",
        r"sudo\s+dd\s+",
        r"\beval\s*\(",
        r"\bexec\s*\(",
        r"kubectl\s+delete\s+",
        r"dd\s+if=.*of=/dev/",
        r"mkfs\.",
        r">\s*/dev/sd[a-z]",
        r"chmod\s+-R\s+777\s+/",
        r":(){ :|:& };:",  # fork bomb
    ]
]


@dataclass(frozen=True)
class PermissionDecision:
    """Result of a permission mode evaluation."""

    allowed: bool
    auto_approved: bool = False
    reason: str = ""


def _check_action_rules(
    tool_name: str,
    args: dict[str, Any],
) -> PermissionDecision | None:
    """Check action-level rules for a tool in read-only context.

    Returns a denial ``PermissionDecision`` if the action is blocked,
    or ``None`` if allowed.
    """
    if tool_name not in READ_ONLY_ACTION_RULES:
        return None
    action = args.get("action")
    if action is None:
        # No action specified — allow (the tool itself validates required params)
        return None
    allowed_actions = READ_ONLY_ACTION_RULES[tool_name]
    if action not in allowed_actions:
        return PermissionDecision(
            allowed=False,
            reason=(
                f"action '{action}' on tool '{tool_name}' not allowed "
                f"in read-only mode (allowed: {', '.join(sorted(allowed_actions))})"
            ),
        )
    return None


class PermissionModeEngine:
    """Evaluates tool execution requests against the active permission mode."""

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT) -> None:
        self._mode = mode

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    @mode.setter
    def mode(self, value: PermissionMode) -> None:
        self._mode = value

    def evaluate(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        """Evaluate whether a tool call should be allowed, auto-approved, or denied.

        Returns a ``PermissionDecision`` indicating the outcome.
        """
        args = tool_args or {}

        # 1. Always check dangerous patterns first.
        is_dangerous, reason = self.is_dangerous(tool_name, args)
        if is_dangerous:
            return PermissionDecision(allowed=False, reason=reason)

        # 2. BYPASS mode: auto-approve everything.
        if self._mode == PermissionMode.BYPASS:
            return PermissionDecision(
                allowed=True,
                auto_approved=True,
                reason="bypass mode",
            )

        # 3. PLAN mode: only read-only tools allowed.
        if self._mode == PermissionMode.PLAN:
            if tool_name in READ_ONLY_TOOLS:
                blocked = _check_action_rules(tool_name, args)
                if blocked:
                    return blocked
                return PermissionDecision(
                    allowed=True,
                    auto_approved=True,
                    reason="plan mode read-only",
                )
            return PermissionDecision(
                allowed=False,
                reason=f"tool '{tool_name}' not allowed in plan mode",
            )

        # 4. ACCEPT_EDITS mode: auto-approve file tools + read tools.
        if self._mode == PermissionMode.ACCEPT_EDITS:
            if tool_name in READ_ONLY_TOOLS or tool_name in FILE_MODIFICATION_TOOLS:
                # Action rules still apply in read-only context
                if tool_name in READ_ONLY_TOOLS:
                    blocked = _check_action_rules(tool_name, args)
                    if blocked:
                        return blocked
                return PermissionDecision(
                    allowed=True,
                    auto_approved=True,
                    reason="accept_edits mode",
                )
            # Other tools (bash, etc.) still need confirmation.
            return PermissionDecision(
                allowed=True,
                auto_approved=False,
                reason="requires confirmation",
            )

        # 5. DEFAULT mode: allow but require confirmation.
        return PermissionDecision(
            allowed=True,
            auto_approved=False,
            reason="default mode",
        )

    def is_dangerous(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Check if a tool call matches a dangerous pattern.

        Only applies to shell execution tools (run_shell, run_command, code_sandbox).
        """
        args = tool_args or {}
        shell_tools = {"run_shell", "run_command", "code_sandbox"}
        if tool_name not in shell_tools:
            return False, ""

        # Check the command/script content against dangerous patterns.
        command_text = str(
            args.get("script", "") or args.get("command", "") or args.get("code", ""),
        )
        if not command_text:
            return False, ""

        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(command_text):
                return True, f"Dangerous pattern detected: {pattern.pattern}"

        return False, ""

    def is_tool_allowed(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
    ) -> bool:
        """Quick check if a tool is allowed under the current mode.

        When *tool_args* is provided, action-level rules are also checked
        for unified tools that have restricted actions in read-only contexts.
        """
        if self._mode == PermissionMode.PLAN:
            if tool_name not in READ_ONLY_TOOLS:
                return False
            if tool_args is not None:
                return _check_action_rules(tool_name, tool_args) is None
        return True

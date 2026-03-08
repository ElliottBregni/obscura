import re

path = "/Users/elliottbregni/dev/obscura-main/obscura/cli/app/modes.py"
with open(path, "r") as f:
    content = f.read()

old = '_MODE_SYSTEM_PROMPTS: dict[TUIMode, str] = {\n    TUIMode.ASK: "",\n    TUIMode.PLAN: (\n        "You are in planning mode. Respond with structured, numbered "\n        "implementation plans. Each step should be actionable and specific. "\n        "Do not write code yet."\n    ),\n    TUIMode.CODE: (\n        "You are in code mode. Use tools to read and write files. "\n        "Show your changes clearly. Explain each change briefly."\n    ),\n    TUIMode.DIFF: (\n        "You are reviewing code changes. Analyze the diffs provided and "\n        "give feedback on correctness, style, and potential issues."\n    ),\n}'

addition = '''


# ---------------------------------------------------------------------------
# Mode capability groups
#
# Maps each TUIMode to the set of tool names available in that mode.
#   None         => all tools (CODE mode)
#   frozenset()  => no tools  (ASK mode)
#   frozenset({...}) => exactly those tool names
#
# Edit the sets below or call ModeManager.set_mode_tools() at runtime
# to customize what each mode can access.
# ---------------------------------------------------------------------------

_DIFF_MODE_TOOLS: frozenset[str] = frozenset({
    # Filesystem (read-only)
    "list_directory", "read_text_file", "grep_files",
    "find_files", "file_info", "tree_directory", "diff_files",
    # Git (inspection only)
    "git_status", "git_diff", "git_log", "git_branch",
    # Utilities
    "context_window_status", "json_query", "clipboard_read", "clipboard_write",
})

_PLAN_MODE_TOOLS: frozenset[str] = frozenset({
    # Filesystem (read-only)
    "list_directory", "read_text_file", "grep_files",
    "find_files", "file_info", "tree_directory",
    # Web research
    "web_fetch", "web_search",
    # System info
    "context_window_status", "get_system_info",
})

MODE_TOOL_GROUPS: dict[TUIMode, "frozenset[str] | None"] = {
    TUIMode.ASK:  frozenset(),       # conversational only — no tools
    TUIMode.PLAN: _PLAN_MODE_TOOLS,  # read + research — no writes/exec
    TUIMode.CODE: None,              # full access — all registered tools
    TUIMode.DIFF: _DIFF_MODE_TOOLS,  # read + git inspection — no writes
}'''

if old in content:
    content = content.replace(old, old + addition, 1)
    with open(path, "w") as f:
        f.write(content)
    print("OK: modes.py patched")
else:
    print("ERROR: old string not found in modes.py")
    # debug: show first 50 chars of what we expect vs what's there
    idx = content.find("_MODE_SYSTEM_PROMPTS")
    print("Found at:", idx)
    print(repr(content[idx:idx+100]))

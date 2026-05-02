"""Tests for the slim native-tools profile.

``OBSCURA_NATIVE_TOOLS`` controls how many built-in obscura tools get
registered as agent-callable. Default ``"slim"`` drops 19 tools that
are either redundant with ``run_shell`` (cp/mv/rm/mkdir/ps/etc),
dynamic-tool-creation (security risk for prod), or MCP debug
(better as ``/mcp`` slash commands). ``"full"`` is the legacy
behaviour that registers everything.

These tests pin down the behaviour so the cut doesn't regress and
critical tools never accidentally land in the drop list.
"""

from __future__ import annotations

import pytest

from obscura.tools.system import (
    _SLIM_NATIVE_TOOLS_DROP,
    get_system_tool_specs,
)


# Tools that MUST be present in ``get_system_tool_specs()`` under the
# slim profile — dropping any of these breaks core agent capability.
#
# Note: ``enter_plan_mode`` and ``exit_plan_mode`` are registered via a
# different code path (not ``get_system_tool_specs``) so they're not in
# this list. The slim filter doesn't touch them either way.
_CRITICAL_TOOLS = frozenset(
    {
        "run_shell",
        "read_text_file",
        "write_text_file",
        "edit_text_file",
        "append_text_file",
        "list_directory",
        "find_files",
        "grep_files",
        "git",
        "diff_files",
        "file_info",
        "tree_directory",
        "web_fetch",
        "web_search",
        "http_request",
        "json_query",
        "ask_user",
        "user_ask",
        "user_interact",
        "tool_search",
        "list_system_tools",
        "context_window_status",
        "todo_write",
        "history_snip",
        "report_intent",
        "config",
    }
)


class TestSlimProfile:
    def test_default_profile_drops_19_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Default (no env var set) must be slim.
        monkeypatch.delenv("OBSCURA_NATIVE_TOOLS", raising=False)
        slim_specs = get_system_tool_specs()
        slim_names = {s.name for s in slim_specs}

        monkeypatch.setenv("OBSCURA_NATIVE_TOOLS", "full")
        full_specs = get_system_tool_specs()
        full_names = {s.name for s in full_specs}

        dropped = full_names - slim_names
        assert dropped == set(_SLIM_NATIVE_TOOLS_DROP), (
            f"Slim profile dropped tools mismatch.\n"
            f"  unexpected drops: {dropped - set(_SLIM_NATIVE_TOOLS_DROP)}\n"
            f"  expected but kept: {set(_SLIM_NATIVE_TOOLS_DROP) - dropped}"
        )
        assert len(_SLIM_NATIVE_TOOLS_DROP) == 19

    def test_critical_tools_never_dropped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity check: the drop list must never contain a tool the
        agent depends on. Catches a future PR that drops something it
        shouldn't."""
        monkeypatch.delenv("OBSCURA_NATIVE_TOOLS", raising=False)
        names = {s.name for s in get_system_tool_specs()}
        missing = _CRITICAL_TOOLS - names
        assert missing == set(), f"Critical tools missing under slim: {missing}"

        # And no critical tool is in the drop list itself.
        accidentally_dropped = _CRITICAL_TOOLS & set(_SLIM_NATIVE_TOOLS_DROP)
        assert accidentally_dropped == set(), (
            f"Critical tools accidentally in drop list: {accidentally_dropped}"
        )

    def test_full_profile_registers_everything(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_NATIVE_TOOLS", "full")
        full_names = {s.name for s in get_system_tool_specs()}
        # All slim-drop tools must reappear under full.
        for name in _SLIM_NATIVE_TOOLS_DROP:
            assert name in full_names, (
                f"{name} expected under 'full' but missing"
            )

    @pytest.mark.parametrize(
        "val",
        ["slim", "SLIM", "", "weird-bogus-value", "true"],
    )
    def test_unknown_profile_falls_back_to_slim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        val: str,
    ) -> None:
        """Anything that isn't ``full`` is treated as slim — that's the
        safe default. Avoids accidentally re-enabling the 19 dropped
        tools because of a typo."""
        if val:
            monkeypatch.setenv("OBSCURA_NATIVE_TOOLS", val)
        else:
            monkeypatch.delenv("OBSCURA_NATIVE_TOOLS", raising=False)
        names = {s.name for s in get_system_tool_specs()}
        # None of the slim-dropped tools should be present.
        for dropped in _SLIM_NATIVE_TOOLS_DROP:
            assert dropped not in names, (
                f"{dropped} should not be in slim profile (env={val!r})"
            )

    def test_drop_list_categories(self) -> None:
        """Sanity-check the drop list reflects the documented categories.

        Catches an accidental edit that adds something off-bucket.
        """
        run_shell_redundant = {
            "run_python3",
            "run_command",
            "which_command",
            "copy_path",
            "move_path",
            "remove_path",
            "make_directory",
            "get_environment",
            "get_system_info",
            "list_processes",
            "signal_process",
            "list_listening_ports",
            "list_unix_capabilities",
            "download_file",
        }
        dynamic_tools = {
            "create_tool",
            "call_dynamic_tool",
            "list_dynamic_tools",
        }
        mcp_debug = {"mcp_discovery_status", "mcp_cleanup_orphans"}

        assert (
            run_shell_redundant | dynamic_tools | mcp_debug
            == set(_SLIM_NATIVE_TOOLS_DROP)
        )

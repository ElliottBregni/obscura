"""Tests for obscura.core.permission_modes — mode enforcement."""

from __future__ import annotations

from obscura.core.permission_modes import PermissionMode, PermissionModeEngine


def test_default_mode_allows_all() -> None:
    engine = PermissionModeEngine(PermissionMode.DEFAULT)
    d = engine.evaluate("read_text_file")
    assert d.allowed
    assert not d.auto_approved
    d = engine.evaluate("run_shell", {"script": "ls"})
    assert d.allowed
    assert not d.auto_approved


def test_plan_mode_read_only() -> None:
    engine = PermissionModeEngine(PermissionMode.PLAN)
    d = engine.evaluate("read_text_file")
    assert d.allowed and d.auto_approved
    d = engine.evaluate("write_text_file")
    assert not d.allowed
    d = engine.evaluate("run_shell", {"script": "ls"})
    assert not d.allowed


def test_accept_edits_auto_approves_files() -> None:
    engine = PermissionModeEngine(PermissionMode.ACCEPT_EDITS)
    d = engine.evaluate("edit_text_file")
    assert d.allowed and d.auto_approved
    d = engine.evaluate("read_text_file")
    assert d.allowed and d.auto_approved
    d = engine.evaluate("run_shell", {"script": "ls"})
    assert d.allowed and not d.auto_approved


def test_bypass_mode_auto_approves_all() -> None:
    engine = PermissionModeEngine(PermissionMode.BYPASS)
    d = engine.evaluate("run_shell", {"script": "ls"})
    assert d.allowed and d.auto_approved


def test_dangerous_patterns_always_denied() -> None:
    for mode in PermissionMode:
        engine = PermissionModeEngine(mode)
        d = engine.evaluate("run_shell", {"script": "rm -rf /"})
        assert not d.allowed, f"rm -rf / should be denied in {mode.value}"
        d = engine.evaluate("run_shell", {"script": "git push --force main"})
        assert not d.allowed, f"git push --force should be denied in {mode.value}"


def test_dangerous_only_for_shell_tools() -> None:
    engine = PermissionModeEngine(PermissionMode.BYPASS)
    d = engine.evaluate("read_text_file", {"path": "rm -rf /"})
    assert d.allowed  # Not a shell tool


def test_mode_switching() -> None:
    engine = PermissionModeEngine(PermissionMode.DEFAULT)
    assert engine.mode == PermissionMode.DEFAULT
    engine.mode = PermissionMode.PLAN
    assert engine.mode == PermissionMode.PLAN
    d = engine.evaluate("write_text_file")
    assert not d.allowed

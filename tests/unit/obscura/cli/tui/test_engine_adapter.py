"""Tests for the engine adapter helpers in ``obscura.cli.tui.engine_adapter``.

We test the pure parts (config validation, _to_session_config,
_build_host_callbacks). ``bootstrap_tui_session`` itself touches the live
composition pipeline and is exercised via integration tests.
"""

from __future__ import annotations

import pytest

from obscura.cli.tui.engine_adapter import (
    TUIEngineConfig,
    _build_host_callbacks,
    _to_session_config,
)
from obscura.composition.session import SessionConfig

pytestmark = pytest.mark.unit


def test_tui_engine_config_validates_with_defaults() -> None:
    cfg = TUIEngineConfig(backend="copilot")
    assert cfg.backend == "copilot"
    assert cfg.full_screen is True
    assert cfg.show_thinking is True
    assert cfg.tools_enabled is True
    assert cfg.confirm_enabled is False
    assert cfg.max_turns == 10


def test_tui_engine_config_is_frozen() -> None:
    cfg = TUIEngineConfig(backend="copilot")
    # ``frozen=True`` Pydantic models raise ValidationError on assignment.
    with pytest.raises((TypeError, ValueError, Exception)) as excinfo:
        cfg.backend = "claude"  # type: ignore[misc]
    # The exception message should reference the immutability/frozen
    # error rather than vague attribute errors.
    msg = str(excinfo.value).lower()
    assert "frozen" in msg or "immutable" in msg or "instance" in msg


def test_to_session_config_projects_typed_fields() -> None:
    cfg = TUIEngineConfig(
        backend="claude",
        model="claude-3-7-sonnet",
        system="be helpful",
        tools_enabled=False,
        confirm_enabled=True,
        max_turns=42,
        no_default_prompt=True,
        supervise=False,
    )
    sc = _to_session_config(cfg, mcp_servers=[])
    assert isinstance(sc, SessionConfig)
    assert sc.backend == "claude"
    assert sc.model == "claude-3-7-sonnet"
    assert sc.system_prompt == "be helpful"
    assert sc.tools_enabled is False
    assert sc.confirm_enabled is True
    assert sc.max_turns == 42
    assert sc.mcp_servers == []
    # Extras carry the supervise / no_default_prompt knobs.
    assert sc.extras.get("supervise") is False
    assert sc.extras.get("no_default_prompt") is True


def test_to_session_config_passes_mcp_servers_through() -> None:
    cfg = TUIEngineConfig(backend="copilot")
    servers = [{"name": "foo", "command": "foo-mcp"}]
    sc = _to_session_config(cfg, mcp_servers=servers)
    assert sc.mcp_servers == servers


def test_build_host_callbacks_omits_none_slots() -> None:
    out = _build_host_callbacks(
        ask_user_cb=None,
        plan_approval_cb=None,
        user_interact_cb=None,
        permission_mode_cb=None,
    )
    assert out == {}


async def _async_noop_str(_prompt: str) -> str:
    return ""


async def _async_noop_bool(_summary: str) -> bool:
    return False


def test_build_host_callbacks_includes_only_set_slots() -> None:
    out = _build_host_callbacks(
        ask_user_cb=_async_noop_str,
        plan_approval_cb=_async_noop_bool,
        user_interact_cb=None,
        permission_mode_cb=None,
    )
    assert set(out.keys()) == {"ask_user_callback", "plan_approval_callback"}
    assert out["ask_user_callback"] is _async_noop_str
    assert out["plan_approval_callback"] is _async_noop_bool

"""Kairos pre-tool guard: background-initiated calls require explicit opt-in.

The strict-typing pass tightened the guard's ``Mapping`` and ``Callable``
generics. The behavioral contract is:

    initiator ∈ {kairos, background, daemon} ∧ ¬kairos_enabled  → vetoed
    otherwise                                                    → allowed
"""

from __future__ import annotations

from obscura.kairos.guards import pre_tool_use_guard


def test_user_initiator_is_always_allowed() -> None:
    allowed, reason = pre_tool_use_guard({"initiator": "user"})
    assert allowed
    assert reason == "allowed"


def test_kairos_initiator_vetoed_without_opt_in() -> None:
    allowed, reason = pre_tool_use_guard({"initiator": "kairos", "session": {}})
    assert not allowed
    assert "vetoed" in reason


def test_background_initiator_vetoed_without_opt_in() -> None:
    allowed, _ = pre_tool_use_guard({"initiator": "background"})
    assert not allowed


def test_daemon_initiator_vetoed_without_opt_in() -> None:
    allowed, _ = pre_tool_use_guard({"initiator": "daemon"})
    assert not allowed


def test_kairos_initiator_allowed_when_session_settings_opt_in() -> None:
    ctx = {
        "initiator": "kairos",
        "session": {"settings": {"kairos_enabled": True}},
    }
    allowed, reason = pre_tool_use_guard(ctx)
    assert allowed
    assert reason == "allowed"


def test_kairos_initiator_allowed_when_nested_kairos_enabled() -> None:
    """Recognises nested ``settings.kairos.enabled`` as the opt-in shape."""
    ctx = {
        "initiator": "kairos",
        "session": {"settings": {"kairos": {"enabled": True}}},
    }
    allowed, _ = pre_tool_use_guard(ctx)
    assert allowed


def test_initiator_case_insensitive() -> None:
    """Initiator matching is case-folded so 'USER'/'User' both work."""
    allowed, _ = pre_tool_use_guard({"initiator": "USER"})
    assert allowed


def test_missing_initiator_defaults_to_user() -> None:
    """No initiator key → defaults to 'user' → allowed."""
    allowed, _ = pre_tool_use_guard({})
    assert allowed

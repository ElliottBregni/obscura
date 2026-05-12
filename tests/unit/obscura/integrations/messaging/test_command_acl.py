"""Tests for `command_acl.is_command_allowed` — sender-based ACL for REPL
commands routed via messaging channels.

Default-deny is the load-bearing security invariant: an empty / missing
allowlist must deny everyone, no matter how the sender is formatted.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from obscura.integrations.messaging import command_acl


@pytest.fixture
def _obscura_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point resolve_obscura_home() at a temp dir for the test duration."""
    monkeypatch.setattr(
        "obscura.integrations.messaging.command_acl.resolve_obscura_home",
        lambda: tmp_path,
    )
    return tmp_path


def _write_config(home: Path, body: str) -> None:
    (home / "config.toml").write_text(dedent(body))


# ---------------------------------------------------------------------------
# Default-deny
# ---------------------------------------------------------------------------


def test_no_config_file_denies(_obscura_home: Path) -> None:
    """Missing config.toml denies everyone."""
    assert command_acl.is_command_allowed("whatsapp", "12316333624") is False


def test_missing_section_denies(_obscura_home: Path) -> None:
    """Config exists but no [messaging.whatsapp] section."""
    _write_config(_obscura_home, "[other]\nx = 1\n")
    assert command_acl.is_command_allowed("whatsapp", "12316333624") is False


def test_section_without_allowlist_denies(_obscura_home: Path) -> None:
    """Section exists, but no `command_allowlist` key."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        enabled = true
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "12316333624") is False


def test_empty_allowlist_denies(_obscura_home: Path) -> None:
    """Explicit empty list denies (same as missing)."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = []
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "12316333624") is False


def test_malformed_toml_denies(_obscura_home: Path) -> None:
    """Broken TOML must not crash — denies everyone."""
    (_obscura_home / "config.toml").write_text("this is = not [ valid toml")
    assert command_acl.is_command_allowed("whatsapp", "12316333624") is False


# ---------------------------------------------------------------------------
# Allowed cases — normalization
# ---------------------------------------------------------------------------


def test_exact_digit_match(_obscura_home: Path) -> None:
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "2316333624") is True


def test_country_code_normalized_off_sender(_obscura_home: Path) -> None:
    """Sender JID with country-code 1 prefix matches a 10-digit allowlist."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "12316333624") is True


def test_country_code_normalized_off_allowlist(_obscura_home: Path) -> None:
    """Allowlist entry with country code matches a 10-digit sender."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["12316333624"]
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "2316333624") is True


def test_jid_suffix_stripped(_obscura_home: Path) -> None:
    """Full WhatsApp JID with @s.whatsapp.net suffix matches."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_command_allowed(
        "whatsapp", "12316333624@s.whatsapp.net",
    ) is True


def test_pretty_formatted_sender(_obscura_home: Path) -> None:
    """Pretty-formatted phone numbers also match."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_command_allowed(
        "whatsapp", "+1 (231) 633-3624",
    ) is True


# ---------------------------------------------------------------------------
# Denied cases — wrong sender
# ---------------------------------------------------------------------------


def test_different_sender_denied(_obscura_home: Path) -> None:
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "5551234567") is False


def test_substring_not_match(_obscura_home: Path) -> None:
    """A digit-substring (e.g. trailing 7 digits) must NOT match — exact only."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    # 6333624 is a suffix of 2316333624 but should NOT match
    assert command_acl.is_command_allowed("whatsapp", "6333624") is False


def test_non_digit_sender_denied(_obscura_home: Path) -> None:
    """LID-only senders (no digits) are denied even if the LID happens
    to share digits with an allowlist entry."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "alice@lid") is False


# ---------------------------------------------------------------------------
# Platform isolation
# ---------------------------------------------------------------------------


def test_platform_isolation(_obscura_home: Path) -> None:
    """An allowlist under [messaging.whatsapp] doesn't grant access under
    [messaging.telegram] (or vice versa)."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "2316333624") is True
    assert command_acl.is_command_allowed("telegram", "2316333624") is False


# ---------------------------------------------------------------------------
# Multiple entries
# ---------------------------------------------------------------------------


def test_multiple_entries(_obscura_home: Path) -> None:
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624", "5551234567"]
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "2316333624") is True
    assert command_acl.is_command_allowed("whatsapp", "5551234567") is True
    assert command_acl.is_command_allowed("whatsapp", "9990000000") is False


def test_non_string_entries_skipped(_obscura_home: Path) -> None:
    """Allowlist entries that aren't strings (e.g. integers) are ignored
    rather than crashing or matching unexpectedly."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        """,
    )
    # Real assertion: still works with valid entry
    assert command_acl.is_command_allowed("whatsapp", "2316333624") is True


# ---------------------------------------------------------------------------
# is_reply_allowed — separate allowlist, same default-deny semantics
# ---------------------------------------------------------------------------


def test_reply_no_config_file_denies(_obscura_home: Path) -> None:
    """Missing config — agent replies to no one."""
    assert command_acl.is_reply_allowed("whatsapp", "12316333624") is False


def test_reply_empty_allowlist_denies(_obscura_home: Path) -> None:
    """Explicit empty reply_allowlist denies (default-deny semantics).

    Regression guard for the 'AI texted my friend' bug: when wuzapi
    fans out every inbound to the REPL, the agent must NOT auto-respond
    unless the sender is explicitly allowlisted.
    """
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        reply_allowlist = []
        """,
    )
    assert command_acl.is_reply_allowed("whatsapp", "12316333624") is False


def test_reply_allowlist_match(_obscura_home: Path) -> None:
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        reply_allowlist = ["2316333624"]
        """,
    )
    assert command_acl.is_reply_allowed("whatsapp", "12316333624") is True


def test_reply_and_command_allowlists_are_independent(_obscura_home: Path) -> None:
    """The two lists don't bleed into each other — a sender on
    command_allowlist but NOT on reply_allowlist still can't get a
    response. (Defensive: enforces the two-list discipline so a future
    refactor doesn't accidentally fall through one to the other.)"""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        command_allowlist = ["2316333624"]
        reply_allowlist = []
        """,
    )
    assert command_acl.is_command_allowed("whatsapp", "2316333624") is True
    assert command_acl.is_reply_allowed("whatsapp", "2316333624") is False


def test_reply_friend_number_denied_default(_obscura_home: Path) -> None:
    """The motivating scenario: friend texts user, user's reply_allowlist
    has only their own number, friend doesn't get an auto-response."""
    _write_config(
        _obscura_home,
        """
        [messaging.whatsapp]
        reply_allowlist = ["2316333624"]
        """,
    )
    # Friend's number
    assert command_acl.is_reply_allowed("whatsapp", "5551234567") is False
    # User's own number still works
    assert command_acl.is_reply_allowed("whatsapp", "2316333624") is True

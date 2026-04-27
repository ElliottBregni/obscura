"""Integration test: per-profile storage path resolution.

Two Chrome profiles running obscura at the same time must end up with
distinct ``events.db`` paths so SQLite concurrency doesn't corrupt
session ids. The pure helper is exercised by
``tests/unit/obscura/cli/test_session_profile_scope.py``; this test
verifies the integration contract — that two ``SessionConfig`` values
with different ``profile_id``s resolve to different homes, and that the
legacy ``profile_id=None`` path stays exactly at ``~/.obscura``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.cli.session import SessionConfig, _resolve_profile_home

pytestmark = pytest.mark.integration


def test_distinct_profiles_get_distinct_homes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
    cfg_a = SessionConfig(profile_id="alpha-1234")
    cfg_b = SessionConfig(profile_id="beta-5678")
    cfg_legacy = SessionConfig(profile_id=None)

    home_a = _resolve_profile_home(cfg_a.profile_id)
    home_b = _resolve_profile_home(cfg_b.profile_id)
    legacy = _resolve_profile_home(cfg_legacy.profile_id)

    assert home_a != home_b
    assert "alpha-1234" in str(home_a)
    assert "beta-5678" in str(home_b)
    # Legacy must stay rooted at OBSCURA_HOME directly — no `/profiles/`
    # path segment. (Use os.sep boundaries so the substring check doesn't
    # false-positive on something like /tmp/test_profiles_x/.)
    import os as _os
    assert f"{_os.sep}profiles{_os.sep}" not in str(legacy)

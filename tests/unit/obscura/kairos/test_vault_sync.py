from __future__ import annotations

import os
from pathlib import Path
import json

import pytest

from obscura.kairos.vault_sync import VaultSync


def test_scan_and_detect_changes(tmp_path: Path):
    vault = tmp_path / "vault"
    user_dir = vault / "user"
    agent_dir = vault / "agent"
    shared_dir = vault / "shared"
    user_dir.mkdir(parents=True)
    agent_dir.mkdir()
    shared_dir.mkdir()

    f1 = user_dir / "note1.md"
    f1.write_text("hello world")
    f2 = shared_dir / "shared.md"
    f2.write_text("shared content")

    vs = VaultSync(vault_dir=vault)
    metas = vs.scan()
    paths = {p.path.name for p in metas}
    assert "note1.md" in paths
    assert "shared.md" in paths

    prev = {}
    changes = vs.detect_changes(prev)
    assert len(changes.added) == 2
    assert len(changes.modified) == 0
    assert len(changes.removed) == 0

    # Modify one file
    f1.write_text("updated")
    prev_hashes = {str(m.path): m.hash for m in metas}
    changes2 = vs.detect_changes(prev_hashes)
    assert any(m.path.name == "note1.md" for m in changes2.modified)
    assert all(m.path.name != "shared.md" for m in changes2.modified)

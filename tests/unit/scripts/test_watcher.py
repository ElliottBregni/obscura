"""Tests for VaultWatcher — fswatch-based file watching."""

from __future__ import annotations

import os
import time
from pathlib import Path

from scripts.sync import DEBOUNCE_SECONDS, VaultSync, VaultWatcher


class TestVaultWatcher:
    """VaultWatcher: watch paths, fswatch command, debounce, lock file."""

    def test_watch_paths_exist(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        """VaultWatcher identifies correct watch paths."""
        watcher = VaultWatcher(vault_path=vault_root, sync=sync_instance)
        paths = watcher.get_watch_paths()
        path_names = [p.name for p in paths]
        assert "repos" in path_names, (
            f"repos/ should be in watch paths, got {path_names}"
        )
        assert "skills" in path_names, (
            f"skills/ should be in watch paths, got {path_names}"
        )
        assert "instructions" in path_names, (
            f"instructions/ should be in watch paths, got {path_names}"
        )

    def test_fswatch_command_structure(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        """fswatch command includes excludes and paths."""
        watcher = VaultWatcher(vault_path=vault_root, sync=sync_instance)
        paths = watcher.get_watch_paths()
        cmd = watcher.build_fswatch_cmd(paths)
        assert cmd[0] == "fswatch", f"Command should start with fswatch, got {cmd[0]}"
        assert "-r" in cmd, "Command should include -r flag"
        assert "--exclude" in cmd, "Command should include --exclude"

    def test_debounce_suppresses_rapid(
        self, sync_instance: VaultSync, vault_root: Path, mock_home: Path
    ) -> None:
        """Rapid changes within debounce window are suppressed."""
        vs = VaultSync(vault_path=vault_root, dry_run=True)
        watcher = VaultWatcher(vault_path=vault_root, sync=vs)
        watcher.last_sync = time.monotonic()  # Pretend we just synced
        # This should be suppressed (within debounce window)
        watcher.handle_change("/some/changed/file.md")
        # Verify it didn't update last_sync (meaning no sync happened)
        elapsed = time.monotonic() - watcher.last_sync
        assert elapsed < DEBOUNCE_SECONDS, "Debounced call should not update last_sync"

    def test_lock_file_lifecycle(
        self, sync_instance: VaultSync, vault_root: Path, mock_lock_file: Path
    ) -> None:
        """Lock file created and removed correctly."""
        watcher = VaultWatcher(vault_path=vault_root, sync=sync_instance)
        watcher.acquire_lock()
        assert mock_lock_file.exists(), "Lock file should be created"
        assert mock_lock_file.read_text().strip() == str(os.getpid()), (
            "Lock file should contain current PID"
        )
        watcher.release_lock()
        assert not mock_lock_file.exists(), "Lock file should be removed"

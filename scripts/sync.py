from pathlib import Path

# Lock file used by tests; monkeypatch fixture overrides it
LOCK_FILE = Path("/tmp/vaultsync.lock")

class VaultSync:
    """Minimal VaultSync stub used by tests' fixtures.

    Only implements the interface expected by tests (constructor).
    """
    def __init__(self, vault_path: Path | str) -> None:
        self.vault_path = Path(vault_path)

    def sync_all(self) -> list:
        # No-op stub
        return []

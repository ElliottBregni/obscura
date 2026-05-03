"""Compatibility shim: re-export VaultSync from its new location.

This module exists so older imports (used by tests) continue to work
during refactors.
"""

from __future__ import annotations

from obscura.kairos.vault_sync import VaultSync

__all__ = ["VaultSync"]

"""Tests for sdk.cli compatibility shim."""

from __future__ import annotations

from obscura.cli import main as obscura_main
from sdk.cli import main as sdk_main


def test_sdk_cli_main_delegates_to_obscura_cli_main() -> None:
    assert sdk_main is obscura_main

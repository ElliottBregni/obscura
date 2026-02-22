"""Compatibility shim for legacy obscura console-script entry points."""

from __future__ import annotations

from obscura.cli.chat_cli import main


if __name__ == "__main__":
    main()

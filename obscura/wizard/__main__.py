"""Entry point for ``python -m obscura.wizard``."""

from __future__ import annotations

import sys

from obscura.wizard.tui import run

if __name__ == "__main__":
    sys.exit(run())

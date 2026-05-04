"""UI-domain enums.

Promotes the loose UI-related literals (diff line tags, TUI mode,
border / banner style) and creates `OutputMode` / `LogFormat` from the
free-form strings used in `config.py` and `core/config.py`.
"""

from __future__ import annotations

from enum import StrEnum


class DiffLineType(StrEnum):
    """Diff line marker. Values match unified-diff prefix bytes."""

    ADD = "+"
    REMOVE = "-"
    CONTEXT = " "


class TUIMode(StrEnum):
    ASK = "ask"
    PLAN = "plan"
    CODE = "code"
    DIFF = "diff"


class BorderStyle(StrEnum):
    NONE = "none"
    LIGHT = "light"
    HEAVY = "heavy"
    ROUND = "round"
    DOUBLE = "double"


class BannerTheme(StrEnum):
    OBSCURA_DEFAULT = "obscura_default"
    OVERHAUL_GREEN_BLUE = "overhaul_green_blue"
    OVERHAUL_ORANGE = "overhaul_orange"
    OBSCURA_BY_OVERHAUL = "obscura_by_overhaul"
    NONE = "none"


class OutputMode(StrEnum):
    """Output renderer selector.

    `CLI` is today's `OBSCURA_OUTPUT_MODE=cli` default. `JSON` and
    `TEXT` cover headless / piped consumers.
    """

    CLI = "cli"
    JSON = "json"
    TEXT = "text"


class LogFormat(StrEnum):
    """Structlog output format.

    `JSON` is the production default; `TEXT` (alias `CONSOLE`) renders
    via `structlog.dev.ConsoleRenderer`. `CONSOLE` is forward-looking
    and not yet emitted on the wire — kept here so the renderer can
    pivot without touching this file.
    """

    JSON = "json"
    TEXT = "text"
    CONSOLE = "console"


__all__ = [
    "BannerTheme",
    "BorderStyle",
    "DiffLineType",
    "LogFormat",
    "OutputMode",
    "TUIMode",
]

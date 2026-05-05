"""obscura.cli.renderer.modern.theme — Catppuccin Mocha color palette.

Single source of truth for all CLI colors.  Every color has both an
ANSI 256-code (for the modern renderer's raw escape sequences) and a
hex string (for Rich markup and prompt_toolkit HTML).

Palette: Catppuccin Mocha — https://catppuccin.com/palette
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Catppuccin Mocha — semantic color palette
# ---------------------------------------------------------------------------
# Each entry: (ansi_256_code, "#hex")
# ANSI 256 codes are the closest match to the Catppuccin hex values.


@dataclass(frozen=True)
class Color:
    """A color with both ANSI 256 and hex representations."""

    ansi: int
    hex: str

    def ansi_fg(self) -> str:
        return f"\033[38;5;{self.ansi}m"

    def rich(self) -> str:
        """Return the hex string for use in Rich markup."""
        return self.hex


# ── Base colors ──────────────────────────────────────────────────────────

TEXT = Color(254, "#cdd6f4")  # main text
SUBTEXT1 = Color(250, "#bac2de")  # secondary text
SUBTEXT0 = Color(246, "#a6adc8")  # tertiary text
OVERLAY2 = Color(243, "#9399b2")  # muted overlay
OVERLAY1 = Color(240, "#7f849c")  # dimmer overlay
OVERLAY0 = Color(245, "#6c7086")  # dimmest overlay
SURFACE2 = Color(236, "#585b70")  # raised surface
SURFACE1 = Color(234, "#45475a")  # default surface
SURFACE0 = Color(233, "#313244")  # sunken surface
BASE = Color(232, "#1e1e2e")  # background
CRUST = Color(232, "#11111b")  # deepest background

# ── Accent colors ────────────────────────────────────────────────────────

BLUE = Color(111, "#89b4fa")  # primary accent (links, active)
LAVENDER = Color(147, "#b4befe")  # prompt character, headings
SAPPHIRE = Color(75, "#74c7ec")  # secondary accent
TEAL = Color(79, "#94e2d5")  # git branches, metadata
GREEN = Color(78, "#a6e3a1")  # success, additions, running
YELLOW = Color(186, "#f9e2af")  # warnings, cautions
PEACH = Color(216, "#fab387")  # medium alerts, waiting
RED = Color(210, "#f38ba8")  # errors, deletions, high usage
MAUVE = Color(141, "#cba6f7")  # thinking, reasoning
PINK = Color(211, "#f5c2e7")  # special highlights
FLAMINGO = Color(210, "#f2cdcd")  # soft accent
ROSEWATER = Color(224, "#f5e0dc")  # warmest neutral
SKY = Color(117, "#89dcfe")  # info, tool results
MAROON = Color(167, "#eba0ac")  # alternate error

# ── Semantic aliases ─────────────────────────────────────────────────────
# These are the names used throughout the codebase.

ACCENT = BLUE.ansi
ACCENT_HEX = BLUE.hex
ACCENT_DIM = SAPPHIRE.ansi
TOOL_COLOR = YELLOW.ansi
TOOL_HEX = YELLOW.hex
THINKING_COLOR = MAUVE.ansi
THINKING_HEX = MAUVE.hex
ERROR_COLOR = RED.ansi
ERROR_HEX = RED.hex
OK_COLOR = GREEN.ansi
OK_HEX = GREEN.hex
WARN_COLOR = PEACH.ansi
WARN_HEX = PEACH.hex
MUTED = OVERLAY0.ansi
MUTED_HEX = OVERLAY0.hex
WHITE = TEXT.ansi
DIM_WHITE = SUBTEXT1.ansi

# Border characters
BORDER_LIGHT = "─│┌┐└┘"
BORDER_HEAVY = "━┃┏┓┗┛"
BORDER_ROUND = "─│╭╮╰╯"
BORDER_DOUBLE = "═║╔╗╚╝"

# Gradient palettes (256-color indices)
ULTRATHINK_GRADIENT = [129, 135, 141, 99, 63, 33, 39, 51, 87, 123]
CYAN_GRADIENT = [23, 30, 37, 44, 51, 87, 123]
WARM_GRADIENT = [196, 202, 208, 214, 220, 226]


@dataclass(frozen=True)
class Style:
    """Immutable style descriptor for rendering cells."""

    fg: int = WHITE
    bg: int = -1  # -1 = default/transparent
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False

    def ansi_fg(self) -> str:
        if self.fg < 0:
            return ""
        return f"\033[38;5;{self.fg}m"

    def ansi_bg(self) -> str:
        if self.bg < 0:
            return ""
        return f"\033[48;5;{self.bg}m"

    def ansi_attrs(self) -> str:
        parts: list[str] = []
        if self.bold:
            parts.append("\033[1m")
        if self.dim:
            parts.append("\033[2m")
        if self.italic:
            parts.append("\033[3m")
        if self.underline:
            parts.append("\033[4m")
        return "".join(parts)

    def ansi(self) -> str:
        return self.ansi_attrs() + self.ansi_fg() + self.ansi_bg()


RESET = "\033[0m"

# Pre-built styles
STYLE_DEFAULT = Style()
STYLE_ACCENT = Style(fg=ACCENT, bold=True)
STYLE_DIM = Style(fg=MUTED, dim=True)
STYLE_TOOL = Style(fg=TOOL_COLOR)
STYLE_THINKING = Style(fg=THINKING_COLOR, italic=True, dim=True)
STYLE_ERROR = Style(fg=ERROR_COLOR, bold=True)
STYLE_OK = Style(fg=OK_COLOR)
STYLE_WARN = Style(fg=WARN_COLOR)

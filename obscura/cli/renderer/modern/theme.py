"""obscura.cli.renderer.modern.theme — Color palette and style constants.

Mirrors the theme from ``obscura.cli.render`` and extends it with
256-color palette entries for gradient effects.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Semantic color names (ANSI 256-color codes)
# ---------------------------------------------------------------------------

ACCENT = 51  # bright cyan
ACCENT_DIM = 37  # cyan
TOOL_COLOR = 227  # bright yellow
THINKING_COLOR = 201  # bright magenta
ERROR_COLOR = 196  # bright red
OK_COLOR = 46  # bright green
WARN_COLOR = 226  # yellow
MUTED = 242  # dim gray
WHITE = 231  # bright white
DIM_WHITE = 250  # light gray

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
        """Return the ANSI escape for foreground color."""
        if self.fg < 0:
            return ""
        return f"\033[38;5;{self.fg}m"

    def ansi_bg(self) -> str:
        """Return the ANSI escape for background color."""
        if self.bg < 0:
            return ""
        return f"\033[48;5;{self.bg}m"

    def ansi_attrs(self) -> str:
        """Return ANSI escapes for bold/dim/italic/underline."""
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
        """Full ANSI sequence for this style."""
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

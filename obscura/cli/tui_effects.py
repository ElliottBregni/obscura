"""
obscura.cli.tui_effects — Terminal UI visual effects.

Provides colorful visual feedback for special modes and events:
  - Ultrathink activation animation
  - Context usage progress bar
  - Effort level badge rendering
  - Turn summary with tool stats
  - Gradient text rendering
"""

from __future__ import annotations

import sys


# ═══════════════════════════════════════════════════════════════════════════
# Color palettes
# ═══════════════════════════════════════════════════════════════════════════

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# Ultrathink gradient: deep purple → electric blue → cyan
ULTRATHINK_GRADIENT = [
    "\033[38;5;129m",  # deep purple
    "\033[38;5;135m",  # purple
    "\033[38;5;141m",  # light purple
    "\033[38;5;99m",   # blue-purple
    "\033[38;5;63m",   # blue
    "\033[38;5;33m",   # bright blue
    "\033[38;5;39m",   # cyan-blue
    "\033[38;5;51m",   # cyan
    "\033[38;5;87m",   # light cyan
    "\033[38;5;123m",  # ice blue
]

# Effort level colors
EFFORT_COLORS = {
    "low": "\033[38;5;242m",      # dim gray
    "medium": "\033[38;5;75m",    # sky blue
    "high": "\033[38;5;214m",     # orange
    "max": "\033[38;5;196m",      # red-hot
}

# Status bar segments
STATUS_OK = "\033[38;5;46m"       # bright green
STATUS_WARN = "\033[38;5;226m"    # yellow
STATUS_CRIT = "\033[38;5;196m"    # red


def ultrathink_banner() -> None:
    """Print a colorful ultrathink activation banner."""
    art = [
        "  ██╗   ██╗██╗  ████████╗██████╗  █████╗ ",
        "  ██║   ██║██║  ╚══██╔══╝██╔══██╗██╔══██╗",
        "  ██║   ██║██║     ██║   ██████╔╝███████║",
        "  ██║   ██║██║     ██║   ██╔══██╗██╔══██║",
        "  ╚██████╔╝███████╗██║   ██║  ██║██║  ██║",
        "   ╚═════╝ ╚══════╝╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝",
        "",
        "  ████████╗██╗  ██╗██╗███╗   ██╗██╗  ██╗",
        "  ╚══██╔══╝██║  ██║██║████╗  ██║██║ ██╔╝",
        "     ██║   ███████║██║██╔██╗ ██║█████╔╝ ",
        "     ██║   ██╔══██║██║██║╚██╗██║██╔═██╗ ",
        "     ██║   ██║  ██║██║██║ ╚████║██║  ██╗",
        "     ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝",
    ]
    gradient = ULTRATHINK_GRADIENT
    sys.stdout.write("\n")
    for row_idx, line in enumerate(art):
        colored = BOLD
        for col_idx, ch in enumerate(line):
            ci = (col_idx // 3 + row_idx) % len(gradient)
            colored += gradient[ci] + ch
        colored += RESET
        sys.stdout.write(colored + "\n")
    sys.stdout.write(f"\n{BOLD}\033[38;5;51m  ⚡ Maximum thinking budget activated ⚡{RESET}\n\n")
    sys.stdout.flush()


def effort_badge(level: str) -> str:
    """Return a colorful effort level badge string."""
    color = EFFORT_COLORS.get(level, EFFORT_COLORS["medium"])
    symbols = {"low": "◇", "medium": "◆", "high": "◆◆", "max": "⚡⚡⚡"}
    sym = symbols.get(level, "◆")
    return f"{BOLD}{color}{sym} {level.upper()}{RESET}"


def context_bar(used_pct: float, width: int = 30) -> str:
    """Render a colored context usage bar.

    Example: ▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░ 60%
    """
    filled = int(used_pct * width)
    empty = width - filled

    if used_pct < 0.6:
        color = STATUS_OK
    elif used_pct < 0.85:
        color = STATUS_WARN
    else:
        color = STATUS_CRIT

    bar = f"{color}{'▓' * filled}{DIM}{'░' * empty}{RESET}"
    pct = f"{used_pct * 100:.0f}%"
    return f"{bar} {pct}"


def turn_summary(
    tools_used: list[str],
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_s: float = 0.0,
) -> str:
    """Render a compact turn summary line.

    Example: ✓ 3 tools (Read ×2, Grep) · 1.2K in/0.5K out · 2.3s
    """
    parts: list[str] = []

    if tools_used:
        # Count tools by name.
        counts: dict[str, int] = {}
        for t in tools_used:
            short = t.replace("_text_file", "").replace("_files", "")
            counts[short] = counts.get(short, 0) + 1
        tool_parts = []
        for name, count in counts.items():
            if count > 1:
                tool_parts.append(f"{name} ×{count}")
            else:
                tool_parts.append(name)
        parts.append(f"{STATUS_OK}✓{RESET} {len(tools_used)} tools ({', '.join(tool_parts[:4])})")
    else:
        parts.append(f"{STATUS_OK}✓{RESET} response")

    if tokens_in > 0 or tokens_out > 0:
        ti = f"{tokens_in / 1000:.1f}K" if tokens_in >= 1000 else str(tokens_in)
        to = f"{tokens_out / 1000:.1f}K" if tokens_out >= 1000 else str(tokens_out)
        parts.append(f"{ti} in/{to} out")

    if duration_s > 0:
        parts.append(f"{duration_s:.1f}s")

    return f"{DIM}  {'  ·  '.join(parts)}{RESET}"


def gradient_text(text: str, palette: list[str] | None = None) -> str:
    """Apply a gradient color to text characters."""
    colors = palette or ULTRATHINK_GRADIENT
    result = BOLD
    for i, ch in enumerate(text):
        ci = i % len(colors)
        result += colors[ci] + ch
    result += RESET
    return result


def thinking_indicator(effort: str = "medium") -> str:
    """Return a thinking indicator string based on effort level."""
    if effort == "max":
        return gradient_text("⟪ ultrathinking ⟫")
    if effort == "high":
        return f"{BOLD}\033[38;5;214m⟪ deep thinking ⟫{RESET}"
    if effort == "low":
        return f"{DIM}⟪ quick ⟫{RESET}"
    return f"\033[38;5;75m⟪ thinking ⟫{RESET}"


def flash_message(text: str, color: str = "\033[38;5;51m") -> None:
    """Print a brief highlighted message."""
    sys.stdout.write(f"\n{BOLD}{color}  {text}{RESET}\n\n")
    sys.stdout.flush()

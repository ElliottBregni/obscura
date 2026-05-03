"""obscura.cli.renderer.modern.components — Component library.

Declarative, composable render components that write to a
:class:`FrameBuffer`.  Each component knows how to measure its natural
size and render itself into an allocated region.
"""

from __future__ import annotations

import textwrap
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from typing_extensions import override

from obscura.cli.renderer.modern.layout import (
    BorderStyle,
    BoxStyle,
    Region,
    get_border_chars,
)
from obscura.cli.renderer.modern.theme import (
    ACCENT,
    DIM_WHITE,
    ERROR_COLOR,
    MUTED,
    OK_COLOR,
    STYLE_ACCENT,
    STYLE_DEFAULT,
    STYLE_DIM,
    STYLE_ERROR,
    STYLE_OK,
    STYLE_TOOL,
    Style,
)

if TYPE_CHECKING:
    from obscura.cli.renderer.modern.frame_buffer import FrameBuffer

# ---------------------------------------------------------------------------
# Base Component
# ---------------------------------------------------------------------------


class Component:
    """Base class for all renderable components."""

    def __init__(
        self,
        *,
        style: BoxStyle | None = None,
        visible: bool = True,
    ) -> None:
        self.style: BoxStyle = style or BoxStyle()
        self.visible: bool = visible
        self.children: list[Component] = []

    def measure(self, available_width: int) -> tuple[int, int]:
        """Return (min_width, natural_height) for layout.

        Default implementation sums children heights.
        """
        total_h = 0
        for child in self.children:
            if child.visible:
                _w, h = child.measure(available_width)
                total_h += h
        return (available_width, total_h)

    def render(self, buf: FrameBuffer, region: Region) -> int:
        """Render into the buffer region.  Return lines consumed."""
        return 0

    def add_child(self, child: Component) -> None:
        self.children.append(child)


# ---------------------------------------------------------------------------
# RootComponent — top-level vertical stacker
# ---------------------------------------------------------------------------


class RootComponent(Component):
    """Top-level container that stacks children vertically."""

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        total_h = 0
        for child in self.children:
            if child.visible:
                _w, h = child.measure(available_width)
                total_h += h
        return (available_width, total_h)

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        cursor_y = region.y
        for child in self.children:
            if not child.visible:
                continue
            _w, h = child.measure(region.width)
            child_region = Region(
                x=region.x,
                y=cursor_y,
                width=region.width,
                height=h,
            )
            child.render(buf, child_region)
            cursor_y += h
        return cursor_y - region.y


# ---------------------------------------------------------------------------
# TextComponent — styled text with word wrap
# ---------------------------------------------------------------------------


class TextComponent(Component):
    """Plain text with word wrapping and a single style."""

    def __init__(
        self,
        text: str = "",
        *,
        text_style: Style | None = None,
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self.text = text
        self.text_style = text_style or STYLE_DEFAULT

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        if not self.text:
            return (0, 0)
        lines = self._wrap(available_width)
        return (available_width, len(lines))

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        if not self.text:
            return 0
        lines = self._wrap(region.width)
        for i, line in enumerate(lines):
            row = region.y + i
            if row >= region.y + region.height:
                break
            buf.write_line(row, region.x, line, self.text_style)
        return min(len(lines), region.height)

    def _wrap(self, width: int) -> list[str]:
        if width <= 0:
            return []
        raw_lines = self.text.split("\n")
        wrapped: list[str] = []
        for raw in raw_lines:
            if not raw:
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(raw, width=width) or [""])
        return wrapped


# ---------------------------------------------------------------------------
# StreamingTextComponent — accumulates text deltas
# ---------------------------------------------------------------------------


class StreamingTextComponent(Component):
    """Accumulates text deltas and renders with a reveal cursor.

    Characters beyond the reveal cursor are not yet visible.  The cursor
    advances each frame by ``chars_per_frame`` (default 12), creating a
    smooth typing effect.  A blinking block cursor (``▌``) is drawn at
    the reveal edge while text is still being revealed.
    """

    # Pacing: characters revealed per render frame
    CHARS_PER_FRAME: int = 12

    def __init__(
        self,
        *,
        text_style: Style | None = None,
        cursor_style: Style | None = None,
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self._buf: list[str] = []
        self.text_style = text_style or STYLE_DEFAULT
        self.cursor_style = cursor_style or STYLE_ACCENT
        # Reveal cursor: how many chars are visible so far
        self._reveal_pos: int = 0
        self._cursor_visible: bool = True

    def append(self, text: str) -> None:
        self._buf.append(text)

    @property
    def text(self) -> str:
        return "".join(self._buf)

    @property
    def fully_revealed(self) -> bool:
        return self._reveal_pos >= len(self.text)

    def advance_reveal(self) -> bool:
        """Advance the reveal cursor.  Returns True if still revealing."""
        total = len(self.text)
        if self._reveal_pos >= total:
            return False
        # Accelerate: reveal faster when there's a large backlog
        backlog = total - self._reveal_pos
        burst = self.CHARS_PER_FRAME
        if backlog > 200:
            burst = max(burst, backlog // 4)
        elif backlog > 80:
            burst = max(burst, backlog // 6)
        self._reveal_pos = min(total, self._reveal_pos + burst)
        return self._reveal_pos < total

    def clear(self) -> None:
        self._buf.clear()
        self._reveal_pos = 0

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        text = self.text[: self._reveal_pos]
        if not text:
            return (0, 0)
        lines = self._wrap(text, available_width)
        return (available_width, len(lines))

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        text = self.text[: self._reveal_pos]
        if not text:
            return 0
        lines = self._wrap(text, region.width)
        for i, line in enumerate(lines):
            row = region.y + i
            if row >= region.y + region.height:
                break
            buf.write_line(row, region.x, line, self.text_style)
        # Draw cursor at reveal edge
        if not self.fully_revealed and self._cursor_visible and lines:
            last_line = lines[-1]
            cursor_row = region.y + len(lines) - 1
            cursor_col = region.x + len(last_line)
            if cursor_col < region.x + region.width:
                buf.write_line(cursor_row, cursor_col, "▌", self.cursor_style)
        return min(len(lines), region.height)

    def toggle_cursor(self) -> None:
        """Toggle cursor visibility for blink effect."""
        self._cursor_visible = not self._cursor_visible

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        if width <= 0:
            return []
        raw_lines = text.split("\n")
        wrapped: list[str] = []
        for raw in raw_lines:
            if not raw:
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(raw, width=width) or [""])
        return wrapped


# ---------------------------------------------------------------------------
# RuleComponent — horizontal separator
# ---------------------------------------------------------------------------


class RuleComponent(Component):
    """Horizontal rule (separator line)."""

    def __init__(
        self,
        *,
        char: str = "─",
        rule_style: Style | None = None,
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self.char = char
        self.rule_style = rule_style or Style(fg=MUTED, dim=True)

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        return (available_width, 1)

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        line = self.char * region.width
        buf.write_line(region.y, region.x, line, self.rule_style)
        return 1


# ---------------------------------------------------------------------------
# PanelComponent — bordered box with title
# ---------------------------------------------------------------------------


class PanelComponent(Component):
    """Bordered panel with optional title.

    Used for thinking/reasoning blocks and tool result display.
    When ``pulse=True``, the border color oscillates between the base
    color and a dimmer shade, creating a breathing effect while the
    model is actively thinking.
    """

    # Pulse palette: cycles through these 256-color indices
    _PULSE_PALETTE: list[int] = [201, 165, 129, 93, 129, 165]

    def __init__(
        self,
        *,
        title: str = "",
        border: BorderStyle = BorderStyle.ROUND,
        border_color: int = ACCENT,
        title_style: Style | None = None,
        content_style: Style | None = None,
        style: BoxStyle | None = None,
        pulse: bool = False,
    ) -> None:
        super().__init__(style=style)
        self.title = title
        self.border = border
        self.border_color = border_color
        self.title_style = title_style or Style(fg=border_color, bold=True)
        self.content_style = content_style or STYLE_DEFAULT
        self.pulse = pulse
        self._pulse_idx: int = 0
        self._birth: float = time.monotonic()
        self._lines: list[str] = []

    def append(self, text: str) -> None:
        """Append text to the panel content."""
        self._lines.append(text)

    @property
    def content(self) -> str:
        return "".join(self._lines)

    def clear(self) -> None:
        self._lines.clear()

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        inner_w = max(0, available_width - 2)  # 2 for left+right border
        text = self.content
        if not text:
            return (available_width, 2)  # just top+bottom border
        wrapped = self._wrap_content(text, inner_w)
        return (available_width, len(wrapped) + 2)  # +2 for borders

    def advance_pulse(self) -> None:
        """Advance the pulse animation index."""
        self._pulse_idx = (self._pulse_idx + 1) % len(self._PULSE_PALETTE)

    @property
    def _effective_border_color(self) -> int:
        if self.pulse:
            return self._PULSE_PALETTE[self._pulse_idx]
        return self.border_color

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        h, v, tl, tr, bl, br = get_border_chars(self.border)
        color = self._effective_border_color
        border_style = Style(fg=color)
        inner_w = max(0, region.width - 2)

        row = region.y

        # Top border with title
        top_line = tl
        if self.title:
            title_display = f" {self.title} "
            remaining = inner_w - len(title_display)
            if remaining > 0:
                top_line += title_display
                top_line += h * remaining
            else:
                top_line += h * inner_w
        else:
            top_line += h * inner_w
        top_line += tr

        buf.write_line(row, region.x, top_line[: region.width], border_style)
        if self.title:
            # Overwrite title portion with title style
            title_start = region.x + 1
            title_display = f" {self.title} "
            buf.write_line(row, title_start, title_display[:inner_w], self.title_style)
        row += 1

        # Content lines
        text = self.content
        wrapped = self._wrap_content(text, max(0, inner_w - 2))  # padding inside
        for line_text in wrapped:
            if row >= region.y + region.height - 1:
                break
            buf.write_line(row, region.x, v, border_style)
            buf.write_line(
                row,
                region.x + 2,
                line_text[: inner_w - 2],
                self.content_style,
            )
            buf.write_line(row, region.x + region.width - 1, v, border_style)
            row += 1

        # Bottom border
        if row < region.y + region.height:
            bottom_line = bl + h * inner_w + br
            buf.write_line(row, region.x, bottom_line[: region.width], border_style)
            row += 1

        return row - region.y

    @staticmethod
    def _wrap_content(text: str, width: int) -> list[str]:
        if width <= 0 or not text:
            return []
        raw_lines = text.split("\n")
        wrapped: list[str] = []
        for raw in raw_lines:
            if not raw.strip():
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(raw, width=width) or [""])
        return wrapped


# ---------------------------------------------------------------------------
# SpinnerComponent — animated spinner glyph
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class SpinnerComponent(Component):
    """Animated braille spinner with status text and elapsed timer.

    Shows: ``⠹ thinking...  3.2s``

    The dots animate (1–3 trailing dots cycle), and an elapsed-time
    badge appears after the first second.
    """

    def __init__(
        self,
        text: str = "",
        *,
        spinner_style: Style | None = None,
        text_style: Style | None = None,
        timer_style: Style | None = None,
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self.text = text
        self.spinner_style = spinner_style or STYLE_ACCENT
        self.text_style = text_style or STYLE_DIM
        self.timer_style = timer_style or Style(fg=MUTED, dim=True)
        self.frame_idx: int = 0
        self._start_time: float = time.monotonic()
        self._dot_phase: int = 0

    def reset_timer(self) -> None:
        self._start_time = time.monotonic()
        self.frame_idx = 0
        self._dot_phase = 0

    def advance(self) -> None:
        self.frame_idx = (self.frame_idx + 1) % len(_SPINNER_FRAMES)
        # Dots cycle every 3 spinner frames
        if self.frame_idx % 3 == 0:
            self._dot_phase = (self._dot_phase + 1) % 3

    @property
    def spinner_char(self) -> str:
        return _SPINNER_FRAMES[self.frame_idx]

    @property
    def _animated_text(self) -> str:
        """Text with cycling trailing dots."""
        base = self.text.rstrip(".")
        dots = "." * (self._dot_phase + 1)
        return base + dots

    @property
    def _elapsed_badge(self) -> str:
        elapsed = time.monotonic() - self._start_time
        if elapsed < 1.0:
            return ""
        if elapsed < 60:
            return f"{elapsed:.1f}s"
        minutes = int(elapsed // 60)
        secs = int(elapsed % 60)
        return f"{minutes}m{secs:02d}s"

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        return (available_width, 1)

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        col = region.x

        # Spinner glyph
        buf.write_line(region.y, col, self.spinner_char, self.spinner_style)
        col += 2

        # Animated status text
        anim_text = self._animated_text
        buf.write_line(
            region.y,
            col,
            anim_text[: region.width - 4],
            self.text_style,
        )
        col += len(anim_text) + 2

        # Elapsed timer badge
        badge = self._elapsed_badge
        if badge and col + len(badge) < region.x + region.width:
            buf.write_line(region.y, col, badge, self.timer_style)

        return 1


# ---------------------------------------------------------------------------
# ToolCallComponent — one-line tool call summary
# ---------------------------------------------------------------------------


class ToolCallComponent(Component):
    """One-line tool call summary with status indicator and fade-in.

    New tool calls start dim and brighten over ``_FADE_FRAMES`` frames.
    Status transitions (running → done/error) trigger a brief flash.
    """

    _FADE_FRAMES: int = 6

    def __init__(
        self,
        summary: str = "",
        *,
        status: str = "running",  # "running" | "done" | "error"
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self.summary = summary
        self.status = status
        self._age: int = 0  # frames since creation

    def advance_age(self) -> None:
        self._age += 1

    @property
    def _opacity(self) -> float:
        """0.0 → 1.0 over _FADE_FRAMES."""
        if self._age >= self._FADE_FRAMES:
            return 1.0
        return self._age / self._FADE_FRAMES

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        return (available_width, 1)

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        if self.status == "error":
            icon = "✘"
            icon_style = STYLE_ERROR
        elif self.status == "done":
            icon = "✔"
            icon_style = STYLE_OK
        else:
            icon = "▶"
            icon_style = STYLE_TOOL

        # Fade-in: during early frames, render with dim style
        fading = self._opacity < 1.0
        if fading:
            icon_style = Style(fg=icon_style.fg, dim=True)

        text_style = STYLE_TOOL if self.status == "running" else STYLE_DIM
        if fading:
            text_style = Style(fg=text_style.fg, dim=True)

        # "  ▶ summary..."
        buf.write_line(region.y, region.x, "  ", STYLE_DEFAULT)
        buf.write_line(region.y, region.x + 2, icon, icon_style)
        buf.write_line(
            region.y,
            region.x + 4,
            self.summary[: region.width - 4],
            text_style,
        )
        return 1


# ---------------------------------------------------------------------------
# DiffComponent — unified diff display
# ---------------------------------------------------------------------------


@dataclass
class DiffLine:
    """A single line in a diff display."""

    tag: str  # "+", "-", " "
    content: str


class DiffComponent(Component):
    """Colored unified diff display for edit tool results."""

    def __init__(
        self,
        lines: list[DiffLine] | None = None,
        *,
        filepath: str = "",
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self.diff_lines: list[DiffLine] = lines or []
        self.filepath = filepath

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        h = len(self.diff_lines)
        if self.filepath:
            h += 1  # header line
        return (available_width, h)

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        row = region.y

        if self.filepath:
            header = f"  {self.filepath}"
            buf.write_line(row, region.x, header[: region.width], STYLE_ACCENT)
            row += 1

        for dl in self.diff_lines:
            if row >= region.y + region.height:
                break
            if dl.tag == "+":
                line_style = Style(fg=OK_COLOR)
                prefix = "+ "
            elif dl.tag == "-":
                line_style = Style(fg=ERROR_COLOR)
                prefix = "- "
            else:
                line_style = STYLE_DIM
                prefix = "  "
            text = prefix + dl.content
            buf.write_line(row, region.x, text[: region.width], line_style)
            row += 1

        return row - region.y


# ---------------------------------------------------------------------------
# SearchResultsComponent — grouped grep results
# ---------------------------------------------------------------------------


class SearchResultsComponent(Component):
    """Grouped search results for grep tool output."""

    def __init__(
        self,
        *,
        pattern: str = "",
        results: list[tuple[str, list[tuple[int, str]]]] | None = None,
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self.pattern = pattern
        # List of (filepath, [(line_no, line_content), ...])
        self.results: list[tuple[str, list[tuple[int, str]]]] = results or []

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        h = 0
        for _filepath, matches in self.results:
            h += 1  # filepath header
            h += len(matches)  # match lines
        return (available_width, max(h, 0))

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        row = region.y
        for filepath, matches in self.results:
            if row >= region.y + region.height:
                break
            buf.write_line(row, region.x, filepath[: region.width], STYLE_ACCENT)
            row += 1
            for line_no, content in matches:
                if row >= region.y + region.height:
                    break
                prefix = f"  {line_no:>4}: "
                buf.write_line(row, region.x, prefix, Style(fg=MUTED))
                buf.write_line(
                    row,
                    region.x + len(prefix),
                    content[: region.width - len(prefix)],
                    STYLE_DEFAULT,
                )
                row += 1
        return row - region.y


# ---------------------------------------------------------------------------
# FileTreeComponent — indented directory listing
# ---------------------------------------------------------------------------


class FileTreeComponent(Component):
    """Indented tree display for directory listings."""

    def __init__(
        self,
        *,
        entries: list[tuple[int, str, bool]] | None = None,
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        # List of (depth, name, is_directory)
        self.entries: list[tuple[int, str, bool]] = entries or []

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        return (available_width, len(self.entries))

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        row = region.y
        for depth, name, is_dir in self.entries:
            if row >= region.y + region.height:
                break
            indent = "  " * depth
            icon = "📁 " if is_dir else "  "
            entry_style = Style(fg=ACCENT, bold=True) if is_dir else STYLE_DEFAULT
            text = f"{indent}{icon}{name}"
            buf.write_line(row, region.x, text[: region.width], entry_style)
            row += 1
        return row - region.y


# ---------------------------------------------------------------------------
# CodeBlockComponent — syntax-highlighted code display
# ---------------------------------------------------------------------------


class CodeBlockComponent(Component):
    """Code block with line numbers and optional language label."""

    def __init__(
        self,
        code: str = "",
        *,
        language: str = "",
        show_line_numbers: bool = True,
        start_line: int = 1,
        style: BoxStyle | None = None,
    ) -> None:
        super().__init__(style=style)
        self.code = code
        self.language = language
        self.show_line_numbers = show_line_numbers
        self.start_line = start_line

    @override
    def measure(self, available_width: int) -> tuple[int, int]:
        lines = self.code.split("\n")
        h = len(lines)
        if self.language:
            h += 1  # language header
        return (available_width, h)

    @override
    def render(self, buf: FrameBuffer, region: Region) -> int:
        row = region.y
        lines = self.code.split("\n")

        if self.language:
            label = f"  {self.language}"
            buf.write_line(row, region.x, label[: region.width], STYLE_DIM)
            row += 1

        gutter_w = 0
        if self.show_line_numbers:
            max_ln = self.start_line + len(lines) - 1
            gutter_w = len(str(max_ln)) + 2  # "123: "

        line_style = Style(fg=DIM_WHITE)
        gutter_style = Style(fg=MUTED, dim=True)

        for i, line in enumerate(lines):
            if row >= region.y + region.height:
                break
            if self.show_line_numbers:
                ln = str(self.start_line + i).rjust(gutter_w - 2)
                buf.write_line(row, region.x, f"{ln}: ", gutter_style)
            buf.write_line(
                row,
                region.x + gutter_w,
                line[: region.width - gutter_w],
                line_style,
            )
            row += 1

        return row - region.y

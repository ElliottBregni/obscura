"""obscura.cli.renderer.modern.frame_buffer — Double-buffered virtual terminal.

Maintains two framebuffers (current and previous).  On each render pass
the engine writes styled text into the current buffer, then
``diff_and_flush`` emits only the ANSI sequences needed to update
changed cells.

Two modes of operation:

* **Inline mode** (default) — append-only.  New content is written below
  the last flushed line.  No cursor-up movement, so it cooperates with
  ``prompt_toolkit.patch_stdout``.

* **Fullscreen mode** — cursor-addressed.  The entire viewport is owned
  by the renderer and updated via absolute positioning.  Used only when
  alt-screen is active.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from typing import IO

from typing import override

from obscura.cli.renderer.modern.theme import RESET, STYLE_DEFAULT, Style


@dataclass
class Cell:
    """A single character cell with style."""

    char: str = " "
    style: Style = field(default_factory=lambda: STYLE_DEFAULT)

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Cell):
            return NotImplemented
        return self.char == other.char and self.style == other.style


class FrameBuffer:
    """Double-buffered virtual terminal screen.

    Parameters
    ----------
    width, height:
        Dimensions.  Pass 0 to auto-detect from terminal size.
    fullscreen:
        When True, use cursor-addressed rendering.  When False (default),
        use append-only inline rendering.

    """

    def __init__(
        self,
        width: int = 0,
        height: int = 0,
        *,
        fullscreen: bool = False,
    ) -> None:
        if width <= 0 or height <= 0:
            ts = shutil.get_terminal_size((80, 24))
            width = width or ts.columns
            height = height or ts.lines
        self.width = width
        self.height = height
        self.fullscreen = fullscreen

        # Current buffer being written to
        self._current: list[list[Cell]] = self._make_grid()
        # Previous buffer (last flushed state)
        self._previous: list[list[Cell]] = self._make_grid()

        # Inline-mode tracking: lines that have been committed to stdout
        self._committed_lines: int = 0
        # Lines populated in the current frame
        self._written_rows: set[int] = set()

    def _make_grid(self) -> list[list[Cell]]:
        return [[Cell() for _ in range(self.width)] for _ in range(self.height)]

    # -- Public API --------------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        """Resize the buffer, preserving content where possible."""
        old_h = self.height
        old_w = self.width
        self.width = width
        self.height = height
        self._current = self._resize_grid(self._current, old_w, old_h, width, height)
        self._previous = self._resize_grid(self._previous, old_w, old_h, width, height)

    @staticmethod
    def _resize_grid(
        grid: list[list[Cell]],
        old_w: int,
        old_h: int,
        new_w: int,
        new_h: int,
    ) -> list[list[Cell]]:
        new_grid: list[list[Cell]] = []
        for row_idx in range(new_h):
            if row_idx < old_h:
                old_row = grid[row_idx]
                if new_w <= old_w:
                    new_grid.append(old_row[:new_w])
                else:
                    new_grid.append(old_row + [Cell() for _ in range(new_w - old_w)])
            else:
                new_grid.append([Cell() for _ in range(new_w)])
        return new_grid

    def clear(self) -> None:
        """Clear the current buffer."""
        self._current = self._make_grid()
        self._written_rows.clear()

    def write_at(
        self,
        row: int,
        col: int,
        text: str,
        style: Style | None = None,
    ) -> int:
        r"""Write styled text at a position.  Returns columns consumed.

        Handles word-wrap by advancing to the next row when the line
        overflows.  Newline characters (``\\n``) advance to the next row.
        """
        if row < 0 or row >= self.height:
            return 0

        s = style or STYLE_DEFAULT
        col_start = col
        r, c = row, col

        for ch in text:
            if ch == "\n":
                self._written_rows.add(r)
                r += 1
                c = col_start
                if r >= self.height:
                    break
                continue

            if c >= self.width:
                # Wrap to next line
                self._written_rows.add(r)
                r += 1
                c = col_start
                if r >= self.height:
                    break

            if 0 <= c < self.width and 0 <= r < self.height:
                self._current[r][c] = Cell(char=ch, style=s)
                c += 1

        if 0 <= r < self.height:
            self._written_rows.add(r)

        return c - col_start

    def write_line(
        self,
        row: int,
        col: int,
        text: str,
        style: Style | None = None,
    ) -> None:
        """Write a single line of text (no newline handling, no wrap)."""
        if row < 0 or row >= self.height:
            return
        s = style or STYLE_DEFAULT
        for i, ch in enumerate(text):
            c = col + i
            if 0 <= c < self.width:
                self._current[row][c] = Cell(char=ch, style=s)
        self._written_rows.add(row)

    def fill_row(self, row: int, char: str = " ", style: Style | None = None) -> None:
        """Fill an entire row with a character and style."""
        if row < 0 or row >= self.height:
            return
        s = style or STYLE_DEFAULT
        for c in range(self.width):
            self._current[row][c] = Cell(char=char, style=s)
        self._written_rows.add(row)

    # -- Flush: emit ANSI to stream ----------------------------------------

    def diff_and_flush(self, stream: IO[str] | None = None) -> None:
        """Compare current vs previous buffer and emit minimal updates.

        In inline mode, only newly written rows are emitted as complete
        lines (no cursor movement).  In fullscreen mode, changed cells
        are updated via cursor addressing.
        """
        out = stream or sys.stdout
        if self.fullscreen:
            self._flush_fullscreen(out)
        else:
            self._flush_inline(out)
        self.swap()

    def _flush_inline(self, out: IO[str]) -> None:
        """Append-only flush: emit new lines below previously committed output."""
        # Find the range of rows that have content
        if not self._written_rows:
            return

        min_row = min(self._written_rows)
        max_row = max(self._written_rows)

        # Only emit rows we haven't committed yet
        start = max(min_row, self._committed_lines)
        if start > max_row:
            return

        buf: list[str] = []
        for row_idx in range(start, max_row + 1):
            line = self._render_row(row_idx)
            buf.append(line)

        output = "\n".join(buf)
        if output:
            out.write(output + "\n")
            out.flush()

        self._committed_lines = max_row + 1

    def _flush_fullscreen(self, out: IO[str]) -> None:
        """Cursor-addressed flush: update only changed cells."""
        buf: list[str] = []
        for row_idx in range(self.height):
            for col_idx in range(self.width):
                cur = self._current[row_idx][col_idx]
                prev = self._previous[row_idx][col_idx]
                if cur != prev:
                    # Move cursor to position (1-indexed)
                    buf.append(f"\033[{row_idx + 1};{col_idx + 1}H")
                    buf.append(cur.style.ansi())
                    buf.append(cur.char)
                    buf.append(RESET)

        if buf:
            out.write("".join(buf))
            out.flush()

    def _render_row(self, row_idx: int) -> str:
        """Render a single row to an ANSI string, trimming trailing spaces."""
        if row_idx < 0 or row_idx >= self.height:
            return ""

        row = self._current[row_idx]

        # Find last non-space cell
        last_content = -1
        for i in range(self.width - 1, -1, -1):
            if row[i].char != " " or row[i].style != STYLE_DEFAULT:
                last_content = i
                break

        if last_content < 0:
            return ""

        parts: list[str] = []
        current_style: Style | None = None

        for i in range(last_content + 1):
            cell = row[i]
            if cell.style != current_style:
                if current_style is not None:
                    parts.append(RESET)
                if cell.style != STYLE_DEFAULT:
                    parts.append(cell.style.ansi())
                current_style = cell.style
            parts.append(cell.char)

        if current_style is not None and current_style != STYLE_DEFAULT:
            parts.append(RESET)

        return "".join(parts)

    def swap(self) -> None:
        """Promote current buffer to previous (for next diff cycle)."""
        self._previous = [
            [Cell(char=c.char, style=c.style) for c in row] for row in self._current
        ]

    # -- Helpers -----------------------------------------------------------

    def get_content_height(self) -> int:
        """Return the number of rows that contain content."""
        if not self._written_rows:
            return 0
        return max(self._written_rows) + 1

    def reset_inline(self) -> None:
        """Reset inline tracking for a new render pass.

        Call this before building a new frame's content so that
        ``_written_rows`` reflects only the current frame.
        """
        self.clear()

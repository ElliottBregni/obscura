"""NativeNotifier — cross-platform notification bridge.

macOS-first implementation using ``osascript``.  Falls back to terminal
bell + styled print on other platforms.

Usage::

    notifier = NativeNotifier()
    await notifier.notify("Agent Alert", "Data ready for review")

    # Modal dialog (macOS) — blocks until user clicks a button
    clicked = await notifier.dialog(
        "Researcher",
        "Conflicting data found. Which source?",
        buttons=["Source A", "Source B", "Skip"],
    )
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Sequence

from obscura.agent.interaction import AttentionPriority

__all__ = ["NativeNotifier"]

logger = logging.getLogger(__name__)

_IS_MACOS = sys.platform == "darwin"


class NativeNotifier:
    """Cross-platform notification bridge.  macOS-first."""

    # -- High-level API -----------------------------------------------------

    async def notify(
        self,
        title: str,
        message: str,
        *,
        priority: AttentionPriority = AttentionPriority.NORMAL,
        sound: bool = True,
    ) -> None:
        """Send a non-blocking notification (banner/toast).

        LOW priority is silently ignored.
        """
        if priority == AttentionPriority.LOW:
            logger.debug("[notify] %s: %s", title, message)
            return
        if _IS_MACOS:
            await self._macos_notification(title, message, sound=sound)
        else:
            self._terminal_notify(title, message, priority)

    async def dialog(
        self,
        title: str,
        message: str,
        buttons: Sequence[str] = ("OK",),
    ) -> str:
        """Show a modal dialog and return the button the user clicked.

        On macOS this is a native ``display dialog``.  On other platforms
        it falls back to a terminal prompt.
        """
        resolved_buttons = list(buttons) if buttons else ["OK"]
        if _IS_MACOS:
            return await self._macos_dialog(title, message, resolved_buttons)
        return self._terminal_dialog(title, message, resolved_buttons)

    async def attention(
        self,
        title: str,
        message: str,
        *,
        priority: AttentionPriority = AttentionPriority.NORMAL,
        actions: Sequence[str] | None = None,
    ) -> str | None:
        """Unified entry point matching :class:`AttentionPriority` semantics.

        Returns the chosen action for CRITICAL (modal), ``None`` otherwise.
        """
        if priority == AttentionPriority.CRITICAL:
            return await self.dialog(title, message, buttons=actions or ("OK",))
        await self.notify(title, message, priority=priority)
        return None

    # -- macOS backends -----------------------------------------------------

    @staticmethod
    async def _macos_notification(
        title: str,
        message: str,
        *,
        sound: bool = True,
    ) -> None:
        """Banner notification via ``osascript``."""
        escaped_title = title.replace('"', '\\"')
        escaped_msg = message.replace('"', '\\"')
        sound_clause = ' sound name "Glass"' if sound else ""
        script = (
            f'display notification "{escaped_msg}" '
            f'with title "{escaped_title}"{sound_clause}'
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            logger.exception("macOS notification failed")

    @staticmethod
    async def _macos_dialog(
        title: str,
        message: str,
        buttons: list[str],
    ) -> str:
        """Modal dialog via ``osascript``.  Returns the clicked button."""
        escaped_title = title.replace('"', '\\"')
        escaped_msg = message.replace('"', '\\"')
        button_list = ", ".join(f'"{b}"' for b in buttons)
        script = (
            f'display dialog "{escaped_msg}" '
            f'with title "{escaped_title}" '
            f"buttons {{{button_list}}} "
            f"default button 1"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            result = stdout.decode("utf-8", errors="replace").strip()
            if "button returned:" in result:
                return result.split("button returned:", 1)[1].strip()
        except Exception:
            logger.exception("macOS dialog failed")
        return buttons[0] if buttons else "OK"

    # -- Fallbacks ----------------------------------------------------------

    @staticmethod
    def _terminal_notify(
        title: str,
        message: str,
        priority: AttentionPriority,
    ) -> None:
        """Terminal bell + styled print for non-macOS."""
        bell = "\a" if priority in (AttentionPriority.HIGH, AttentionPriority.CRITICAL) else ""
        print(f"{bell}\033[1m[{title}]\033[0m {message}", flush=True)

    @staticmethod
    def _terminal_dialog(
        title: str,
        message: str,
        buttons: list[str],
    ) -> str:
        """Blocking terminal prompt.  Returns the chosen button."""
        print(f"\033[1m[{title}]\033[0m {message}", flush=True)
        if len(buttons) == 1:
            input(f"Press Enter to continue [{buttons[0]}]: ")
            return buttons[0]
        options = " / ".join(f"[{i + 1}] {b}" for i, b in enumerate(buttons))
        choice = input(f"{options}: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(buttons):
                return buttons[idx]
        except ValueError:
            # Try matching by name
            for b in buttons:
                if b.lower() == choice.lower():
                    return b
        return buttons[0]

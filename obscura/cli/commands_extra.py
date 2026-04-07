"""Extra CLI commands (small, self-contained handlers).

This file registers additional slash commands by mutating the
obscura.cli.commands.COMMANDS registry at import time. Kept minimal to
avoid touching the large commands.py file.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import Any

# Lazy imports to avoid heavy module-level dependencies


async def cmd_caffeinate(args: str, ctx: Any) -> str | None:
    """Prevent the machine from sleeping.

    Usage:
        /caffeinate start    # spawn system 'caffeinate' on macOS or a fallback loop
        /caffeinate stop     # stop the background process/task
        /caffeinate status   # show current status

    Best-effort: uses system 'caffeinate' on macOS when available.
    """
    parts = args.strip().split()
    if not parts:
        from obscura.cli.render import print_info

        print_info("Usage: /caffeinate start|stop|status")
        return None

    action = parts[0].lower()

    from obscura.cli.render import print_info, print_ok, print_error

    proc = getattr(ctx, "_caffeinate", None)

    if action in ("start", "on"):
        if proc is not None:
            print_info("caffeinate already running")
            return None

        if sys.platform == "darwin" and shutil.which("caffeinate"):
            try:
                p = await asyncio.create_subprocess_exec("caffeinate", "-dims")
                ctx._caffeinate = p
                print_ok(f"Started caffeinate (pid {getattr(p, 'pid', '?')})")
            except Exception as e:
                print_error(f"Failed to start caffeinate: {e}")
        else:
            async def _keep_awake():
                try:
                    while True:
                        await asyncio.sleep(60)
                except asyncio.CancelledError:
                    return

            task = asyncio.create_task(_keep_awake())
            ctx._caffeinate = task
            print_ok("Started keep-awake background task")

        return None

    elif action in ("stop", "off"):
        if proc is None:
            print_info("caffeinate not running")
            return None

        try:
            if hasattr(proc, "terminate") or hasattr(proc, "kill"):
                try:
                    proc.terminate()
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            else:
                proc.cancel()
                try:
                    await proc
                except asyncio.CancelledError:
                    pass
        except Exception as e:
            print_error(f"Error stopping caffeinate: {e}")

        ctx._caffeinate = None
        print_ok("Stopped caffeinate")
        return None

    elif action in ("status", "sts"):
        if proc is None:
            print_info("caffeinate not running")
        else:
            if hasattr(proc, "pid"):
                print_info(f"caffeinate running (pid {getattr(proc, 'pid', '?')})")
            else:
                print_info("keep-awake background task running")
        return None

    else:
        print_info("Usage: /caffeinate start|stop|status")
        return None


# Register into the main COMMANDS registry if available.
try:
    from obscura.cli.commands import COMMANDS

    COMMANDS["caffeinate"] = cmd_caffeinate
except Exception:
    # Import-time failures are tolerated; the CLI will still work without this command
    pass

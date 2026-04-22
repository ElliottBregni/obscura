"""CLI commands for managing the Obscura browser extension.

Registered as ``obscura browser <subcommand>``.

Subcommands
-----------
install
    Run packages/browser-extension/native-host/install.sh with the pinned
    extension id. Writes the native-messaging manifest into every
    Chrome-family browser dir on the machine and kills any stale host
    processes.
status
    Report whether the native host manifest is installed, the resolved
    python, and any running host processes.
reload
    Kill any running obscura_native_host.py processes so Chrome respawns
    them with the current launcher/env on the next message.
logs
    Tail the native host log (``~/.obscura/logs/browser-extension-host.log``).
id
    Print the pinned extension id. Useful for copy-paste into
    chrome://extensions debugging flows.
open
    Open chrome://extensions in the default browser.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import click


def _repo_root() -> Path:
    """Locate the obscura repo root starting from this file.

    Browser-extension assets live at ``<repo>/packages/browser-extension``.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "packages" / "browser-extension").is_dir():
            return parent
    # Fallback: assume src-layout sibling.
    return here.parents[2]


def _ext_dir() -> Path:
    return _repo_root() / "packages" / "browser-extension"


def _ext_id() -> str | None:
    path = _ext_dir() / ".keys" / "EXTENSION_ID"
    if not path.is_file():
        return None
    return path.read_text().strip() or None


def _host_log_path() -> Path:
    home = Path(os.environ.get("OBSCURA_HOME") or (Path.home() / ".obscura"))
    return home / "logs" / "browser-extension-host.log"


def _native_manifest_paths() -> list[Path]:
    """All locations where the installer may have written the manifest."""
    platform = cast("str", sys.platform)
    if platform.startswith("darwin"):
        base = Path.home() / "Library" / "Application Support"
        browsers = [
            "Google/Chrome",
            "Google/Chrome Canary",
            "Chromium",
            "BraveSoftware/Brave-Browser",
            "Microsoft Edge",
            "Arc/User Data",
            "Vivaldi",
        ]
    elif platform.startswith("linux"):
        base = Path.home() / ".config"
        browsers = [
            "google-chrome",
            "chromium",
            "BraveSoftware/Brave-Browser",
            "microsoft-edge",
            "vivaldi",
        ]
    else:
        return []
    return [
        base / b / "NativeMessagingHosts" / "com.obscura.host.json"
        for b in browsers
    ]


def _running_hosts() -> list[tuple[int, str]]:
    """Return (pid, cmdline) tuples for each running native host process."""
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    out: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines():
        if "obscura_native_host.py" not in line:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        out.append((int(parts[0]), parts[1]))
    return out


# ---------------------------------------------------------------------------
# Click group


@click.group(name="browser")
def browser_group() -> None:
    """Manage the Obscura browser extension."""


@browser_group.command("id")
def cmd_id() -> None:
    """Print the pinned extension id."""
    ext = _ext_id()
    if not ext:
        click.echo("no pinned id (missing .keys/EXTENSION_ID)", err=True)
        sys.exit(1)
    click.echo(ext)


@browser_group.command("install")
def cmd_install() -> None:
    """Install the native-messaging host manifest."""
    installer = _ext_dir() / "native-host" / "install.sh"
    if not installer.is_file():
        click.echo(f"installer not found: {installer}", err=True)
        sys.exit(1)
    rc = subprocess.call([str(installer)], cwd=str(installer.parent))
    if rc != 0:
        sys.exit(rc)
    ext = _ext_id()
    click.echo()
    click.echo("Next: load the unpacked extension if you haven't yet.")
    click.echo(f"  1. Open chrome://extensions")
    click.echo("  2. Enable Developer mode")
    click.echo(f"  3. Load unpacked → {_ext_dir()}")
    if ext:
        click.echo(f"  4. Confirm the id matches: {ext}")


@browser_group.command("status")
def cmd_status() -> None:
    """Report extension install status."""
    ext = _ext_id() or "(unpinned)"
    click.echo(f"extension id     : {ext}")

    installed = [p for p in _native_manifest_paths() if p.is_file()]
    if not installed:
        click.echo("native manifest  : NOT INSTALLED (run `obscura browser install`)")
    else:
        for p in installed:
            click.echo(f"native manifest  : {p}")

    launcher = _ext_dir() / "native-host" / "obscura-native-host"
    if launcher.is_file():
        click.echo(f"launcher         : {launcher}")
    else:
        click.echo("launcher         : missing (re-run install)")

    running = _running_hosts()
    if not running:
        click.echo("host process(es) : none running")
    else:
        for pid, cmd in running:
            click.echo(f"host process     : pid={pid}  {cmd}")

    log = _host_log_path()
    if log.is_file():
        click.echo(f"host log         : {log} ({log.stat().st_size} bytes)")
    else:
        click.echo("host log         : (none yet)")


@browser_group.command("reload")
@click.option("--wait", default=0.0, help="Seconds to wait after kill.")
def cmd_reload(wait: float) -> None:
    """Kill running host processes so Chrome respawns them."""
    running = _running_hosts()
    if not running:
        click.echo("no native host processes running")
        return
    for pid, _ in running:
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(f"sent SIGTERM to {pid}")
        except ProcessLookupError:
            pass
    if wait:
        time.sleep(wait)
    click.echo(
        "Reload the Obscura card on chrome://extensions so the service "
        "worker reconnects."
    )


@browser_group.command("logs")
@click.option("-f", "--follow", is_flag=True, help="Follow the log (tail -f).")
@click.option("-n", "--lines", default=40, show_default=True, help="Tail lines.")
def cmd_logs(follow: bool, lines: int) -> None:
    """Show the native host log."""
    log = _host_log_path()
    if not log.is_file():
        click.echo(f"no log at {log}")
        return
    tail = shutil.which("tail") or "/usr/bin/tail"
    args = [tail, "-n", str(lines)]
    if follow:
        args.append("-f")
    args.append(str(log))
    os.execvp(args[0], args)


@browser_group.command("open")
def cmd_open() -> None:
    """Open chrome://extensions in the default browser."""
    # `open` on macOS / xdg-open on Linux.
    url = "chrome://extensions"
    opener = shutil.which("open") or shutil.which("xdg-open")
    if not opener:
        click.echo(url)
        return
    subprocess.call([opener, url])

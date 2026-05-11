"""``obscura whatsapp`` — manage the wuzapi sidecar + WhatsApp link.

Subcommands::

    obscura whatsapp install      # clone, build, install LaunchAgent
    obscura whatsapp link         # re-trigger QR scan
    obscura whatsapp status       # session + service state
    obscura whatsapp logs         # tail wuzapi log
    obscura whatsapp send TARGET TEXT
    obscura whatsapp uninstall    # remove LaunchAgent (state preserved by default)

This is the user-facing surface that wraps :mod:`install`, :mod:`lifecycle`,
:mod:`setup`, and the typed clients. Each command is intentionally thin —
real logic lives in the wuzapi subpackage. The commands' job is to
arrange the modules and present results.

All commands are safe to run when ``[messaging.whatsapp]`` is *not*
enabled in config.toml; they manipulate the wuzapi sidecar's
infrastructure (binary, LaunchAgent, WhatsApp link) but don't touch
obscura's REPL inbox until you flip the config flag.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from obscura.integrations.whatsapp.wuzapi import lifecycle as lc
from obscura.integrations.whatsapp.wuzapi.client import (
    WuzapiAdminClient,
    WuzapiClient,
    WuzapiError,
)
from obscura.integrations.whatsapp.wuzapi.install import (
    InstallError,
    install as do_install,
    uninstall as do_uninstall,
)
from obscura.integrations.whatsapp.wuzapi.models import WuzapiSendTextRequest
from obscura.integrations.whatsapp.wuzapi.setup import (
    QRArtifacts,
    ensure_user,
    link_session,
    load_admin_token,
    load_user_token,
)

_DEFAULT_BASE_URL = "http://127.0.0.1:18793"


def _run(coro: object) -> object:
    """Tiny helper so the commands can stay sync-bodied."""
    return asyncio.run(coro)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("whatsapp")
def whatsapp_group() -> None:
    """Manage the wuzapi sidecar and WhatsApp link for Obscura."""


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@whatsapp_group.command("install")
@click.option(
    "--port",
    type=int, default=18793, show_default=True,
    help="Loopback port the wuzapi sidecar will listen on.",
)
@click.option(
    "--force-rebuild", is_flag=True,
    help="Rebuild the Go binary even if it's already present.",
)
@click.option(
    "--skip-start", is_flag=True,
    help="Don't kickstart the LaunchAgent after install (manual control).",
)
def install_cmd(port: int, force_rebuild: bool, skip_start: bool) -> None:
    """Clone, build, and install the wuzapi sidecar as a LaunchAgent.

    Idempotent: re-running after a successful install only does what's
    needed (rebuild if missing, write plist if changed, etc.).
    """
    click.echo("Installing wuzapi sidecar…")
    try:
        report = do_install(port=port, force_rebuild=force_rebuild)
    except InstallError as exc:
        click.secho(f"install failed: {exc}", fg="red", err=True)
        sys.exit(1)

    click.echo(
        f"  cloned={report.cloned}  built={report.built}  "
        f"plist={report.plist_written}  secrets={report.secrets_generated}"
    )

    if skip_start:
        click.echo("Skipping LaunchAgent start (run `launchctl load -w …` manually).")
        return

    try:
        lc.load()
        status = lc.kickstart(restart=True)
    except lc.LifecycleError as exc:
        click.secho(f"failed to start LaunchAgent: {exc}", fg="red", err=True)
        sys.exit(1)

    if status.is_running:
        click.secho(f"wuzapi running on 127.0.0.1:{port} (pid={status.pid})", fg="green")
        click.echo("Next: `obscura whatsapp link` to scan QR and link your WhatsApp account.")
    else:
        click.secho("wuzapi loaded but not yet running — check logs.", fg="yellow")


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


@whatsapp_group.command("link")
@click.option(
    "--name", default="obscura", show_default=True,
    help="wuzapi user slot name for this WhatsApp account.",
)
@click.option(
    "--timeout", type=float, default=180.0, show_default=True,
    help="Seconds to wait for the QR scan before giving up.",
)
def link_cmd(name: str, timeout: float) -> None:
    """Create the wuzapi user (if missing) and walk through QR linking."""

    async def _go() -> None:
        async with WuzapiAdminClient(admin_token=load_admin_token()) as admin:
            user = await ensure_user(admin, name=name)
        click.echo(f"wuzapi user: {user.name} (id={user.id[:8]}…)")
        if user.logged_in:
            click.secho(
                f"already linked: jid={user.jid}  no scan needed.", fg="green"
            )
            return

        async with WuzapiClient(token=user.token) as client:
            async def on_qr(qr: QRArtifacts) -> None:
                click.echo(f"QR saved to {qr.png_path}")
                if qr.ascii_text:
                    click.echo("Scan with your phone (or use the PNG):\n")
                    click.echo(qr.ascii_text)
                else:
                    click.echo("Opening QR in Preview (macOS)…")

            try:
                status = await link_session(
                    client, on_qr=on_qr, timeout_s=timeout
                )
            except TimeoutError:
                click.secho("Timed out waiting for QR scan.", fg="red", err=True)
                sys.exit(1)
            click.secho(f"linked: jid={status.jid}", fg="green")

    _run(_go())


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@whatsapp_group.command("status")
def status_cmd() -> None:
    """Show wuzapi service state + WhatsApp session state."""
    svc = lc.status()
    click.echo("Service")
    click.echo(f"  state:           {svc.state}")
    click.echo(f"  pid:             {svc.pid}")
    click.echo(f"  last_exit:       {svc.last_exit_status}")
    click.echo(f"  plist_installed: {lc.is_plist_installed()}")

    try:
        token = load_user_token()
    except RuntimeError:
        click.secho("No wuzapi user yet — run `obscura whatsapp link` first.",
                    fg="yellow")
        return

    async def _probe() -> None:
        async with WuzapiClient(token=token) as c:
            try:
                s = await c.session_status()
            except WuzapiError as exc:
                click.secho(f"  session probe failed: {exc}", fg="red")
                return
            click.echo("WhatsApp session")
            click.echo(f"  connected: {s.connected}")
            click.echo(f"  loggedIn:  {s.logged_in}")
            click.echo(f"  jid:       {s.jid or '(none)'}")
            click.echo(f"  events:    {s.events}")

    _run(_probe())


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


@whatsapp_group.command("logs")
@click.option(
    "-n", "--lines", type=int, default=50, show_default=True,
    help="Number of lines from the tail of each log file.",
)
def logs_cmd(lines: int) -> None:
    """Print the last N lines of the wuzapi stdout/stderr logs."""
    click.echo(lc.tail_log(lines=lines))


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


@whatsapp_group.command("send")
@click.argument("target")
@click.argument("text")
def send_cmd(target: str, text: str) -> None:
    """Send a WhatsApp text message via the linked wuzapi user.

    TARGET should be a phone number (digits, optionally with +).
    """
    import re
    phone = re.sub(r"\D", "", target)
    if not phone:
        click.secho(f"invalid target: {target!r}", fg="red", err=True)
        sys.exit(1)

    async def _go() -> None:
        async with WuzapiClient(token=load_user_token()) as c:
            try:
                resp = await c.send_text(WuzapiSendTextRequest(phone=phone, body=text))
            except WuzapiError as exc:
                click.secho(f"send failed: {exc}", fg="red", err=True)
                sys.exit(1)
        click.secho(f"sent: id={resp.id} timestamp={resp.timestamp}", fg="green")

    _run(_go())


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


@whatsapp_group.command("daemon")
@click.option(
    "--webhook-port", type=int, default=18794, show_default=True,
    help="Loopback port the inbound webhook receiver listens on.",
)
@click.option(
    "--auto-configure-webhook/--no-auto-configure-webhook", default=True,
    help="Reconfigure wuzapi's webhook URL to point at this receiver on startup.",
)
def daemon_cmd(webhook_port: int, auto_configure_webhook: bool) -> None:
    """Run the wuzapi→REPL bridge in the foreground.

    Listens for inbound wuzapi events and injects them into obscura's
    REPL queue via UDS fan-out. Run in a separate terminal while your
    REPL is up; messages will appear as REPL input.

    Ctrl-C to stop.
    """
    from obscura.integrations.whatsapp.wuzapi.service import wuzapi_service

    async def _go() -> None:
        # Optionally make sure wuzapi knows where to POST.
        if auto_configure_webhook:
            try:
                async with WuzapiClient(token=load_user_token()) as c:
                    await c.set_webhook(
                        f"http://127.0.0.1:{webhook_port}/inbound",
                        events=["Message"],
                    )
                click.echo(f"wuzapi webhook -> http://127.0.0.1:{webhook_port}/inbound")
            except WuzapiError as exc:
                click.secho(
                    f"warning: could not auto-configure webhook: {exc}", fg="yellow",
                )

        click.echo(f"wuzapi daemon: listening on 127.0.0.1:{webhook_port}/inbound")
        click.echo("Inbound WhatsApp messages will be injected into the REPL queue.")
        click.echo("Ctrl-C to stop.")
        try:
            async with wuzapi_service(webhook_port=webhook_port):
                # Block until interrupted
                while True:
                    await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            click.echo("\nstopped.")

    try:
        _run(_go())
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


@whatsapp_group.command("uninstall")
@click.option(
    "--wipe-state", is_flag=True,
    help="Also delete ~/.obscura/wuzapi/ (session DB, secrets — irrecoverable).",
)
@click.confirmation_option(prompt="Stop wuzapi and remove LaunchAgent?")
def uninstall_cmd(wipe_state: bool) -> None:
    """Stop wuzapi, remove the LaunchAgent (optionally wipe all state)."""
    do_uninstall(wipe_state=wipe_state)
    click.secho(
        "uninstalled."
        + (" State wiped." if wipe_state else " State preserved at ~/.obscura/wuzapi/."),
        fg="green",
    )


__all__ = ["whatsapp_group"]

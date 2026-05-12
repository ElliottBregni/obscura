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
import logging
import re
import sys
from collections.abc import Coroutine
from typing import Any

import click

from obscura.composition.a2a import build_a2a_session
from obscura.composition.session import SessionConfig
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.whatsapp.wuzapi import lifecycle as lc
from obscura.integrations.whatsapp.wuzapi.adapter import WuzapiAdapter
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
from obscura.integrations.whatsapp.wuzapi.service import AutoResponder, wuzapi_service
from obscura.integrations.whatsapp.wuzapi.setup import (
    QRArtifacts,
    ensure_user,
    link_session,
    load_admin_token,
    load_user_token,
)


_DEFAULT_BASE_URL = "http://127.0.0.1:18793"


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Tiny helper so the commands can stay sync-bodied."""
    return asyncio.run(coro)


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


_DEFAULT_AUTO_RESPOND_SYSTEM_PROMPT = (
    "You are a helpful WhatsApp assistant replying to Elliott via WhatsApp. "
    "Be concise (under 300 characters). "
    "No markdown, no bullet points — just natural conversational text. "
    "If you don't know something, say so briefly."
)


@whatsapp_group.command("daemon")
@click.option(
    "--webhook-port", type=int, default=18794, show_default=True,
    help="Loopback port the inbound webhook receiver listens on.",
)
@click.option(
    "--auto-configure-webhook/--no-auto-configure-webhook", default=True,
    help="Reconfigure wuzapi's webhook URL to point at this receiver on startup.",
)
@click.option(
    "--auto-respond/--no-auto-respond", default=False,
    help="Generate an LLM reply for each inbound message and send it back via "
         "WhatsApp. When off, inbound messages only land in the REPL queue.",
)
@click.option(
    "--auto-respond-backend",
    type=click.Choice(["claude", "copilot", "openai", "moonshot", "localllm"]),
    default="claude", show_default=True,
    help="Which backend powers the auto-reply when --auto-respond is on.",
)
@click.option(
    "--auto-respond-system-prompt", default=_DEFAULT_AUTO_RESPOND_SYSTEM_PROMPT,
    help="System prompt for the auto-respond agent.",
)
@click.option(
    "--auto-respond-timeout", type=float, default=60.0, show_default=True,
    help="Max seconds to wait for the LLM reply before giving up.",
)
@click.option(
    "--auto-respond-max-chars", type=int, default=1000, show_default=True,
    help="Truncate replies to this many characters before sending (WhatsApp limit ~4096).",
)
def daemon_cmd(
    webhook_port: int,
    auto_configure_webhook: bool,
    auto_respond: bool,
    auto_respond_backend: str,
    auto_respond_system_prompt: str,
    auto_respond_timeout: float,
    auto_respond_max_chars: int,
) -> None:
    """Run the wuzapi→REPL bridge in the foreground.

    Listens for inbound wuzapi events. Each event lands in the REPL inject
    queue via UDS fan-out (so any running ``obscura`` REPL can consume it).

    With ``--auto-respond``, the daemon also runs an in-process agent
    loop per inbound message and sends the reply back via WhatsApp. No
    REPL involvement required — use this if you want a "personal
    assistant that texts you back" rather than a REPL companion.

    Ctrl-C to stop.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        force=True,
    )
    logging.getLogger("obscura.integrations.whatsapp.wuzapi").setLevel(logging.INFO)

    # Build the optional auto-responder closure now so the daemon entry
    # point is small + linear.
    responder = (
        _build_auto_responder(
            backend=auto_respond_backend,
            system_prompt=auto_respond_system_prompt,
            timeout_s=auto_respond_timeout,
            max_chars=auto_respond_max_chars,
        )
        if auto_respond
        else None
    )

    async def _go() -> None:
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
        if auto_respond:
            click.echo(
                f"auto-respond: ON (backend={auto_respond_backend}, "
                f"timeout={auto_respond_timeout:g}s)"
            )
        click.echo("Ctrl-C to stop.")
        try:
            async with wuzapi_service(
                webhook_port=webhook_port,
                auto_responder=responder,
            ):
                while True:
                    await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            click.echo("\nstopped.")

    try:
        _run(_go())
    except KeyboardInterrupt:
        pass


def _build_auto_responder(
    *,
    backend: str,
    system_prompt: str,
    timeout_s: float,
    max_chars: int,
) -> AutoResponder:
    """Construct the (message, adapter) → reply-via-WhatsApp closure.

    Each call builds a fresh AgentSession via obscura's composition.a2a,
    runs the message through the agent loop with a short ``max_turns``
    budget, then sends the resulting text back to the inbound sender.
    Cap on response time prevents a hung LLM from blocking subsequent
    inbounds — we just skip the reply in that case.
    """

    async def respond(msg: PlatformMessage, adapter: WuzapiAdapter) -> None:
        config = SessionConfig(
            backend=backend,
            model=None,
            system_prompt=system_prompt,
            max_turns=3,
        )
        try:
            async with await build_a2a_session(
                config, task_id=f"wapp-{msg.message_id[:12]}",
            ) as session:
                result = await asyncio.wait_for(
                    session.run_loop_to_text(msg.text),
                    timeout=timeout_s,
                )
        except TimeoutError:
            print(
                f"[wuzapi auto-respond] timed out (>{timeout_s:g}s) for {msg.sender_id}",
                flush=True,
            )
            return

        reply = (result or "").strip()
        if not reply:
            print(
                f"[wuzapi auto-respond] empty LLM reply for {msg.sender_id}, skipping",
                flush=True,
            )
            return
        if len(reply) > max_chars:
            reply = reply[: max_chars - 1].rstrip() + "…"
        sent = await adapter.send(msg.sender_id, reply)
        print(
            f"[wuzapi auto-respond] reply to={msg.sender_id} "
            f"chars={len(reply)} sent={sent}",
            flush=True,
        )

    return respond


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

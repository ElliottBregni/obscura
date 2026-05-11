"""obscura.integrations.network_gateway.tailscale — Tailscale serve helpers.

Non-fatal wrappers around the ``tailscale`` CLI.  If tailscale is not in
PATH, all functions log a warning and return safe defaults (False / None / {}).

Typical usage in the gateway lifespan::

    from obscura.integrations.network_gateway.tailscale import (
        configure_tailscale_serve,
        remove_tailscale_serve,
        detect_tailscale_url,
    )

    if config.tailscale_enabled:
        ok = await configure_tailscale_serve(config.port)
        if ok:
            url = detect_tailscale_url() or config.tailscale_url
            logger.info("Gateway also reachable at %s", url)

    # ... on shutdown ...
    await remove_tailscale_serve(config.port)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any, cast

logger = logging.getLogger(__name__)

_TAILSCALE_BIN = "tailscale"


def _tailscale_available() -> bool:
    """Return True if the tailscale CLI is in PATH."""
    return shutil.which(_TAILSCALE_BIN) is not None


async def configure_tailscale_serve(
    port: int,
    *,
    listen_port: int | None = None,
    funnel: bool = False,
) -> bool:
    """Expose ``localhost:{port}`` via Tailscale serve (or funnel).

    Runs::

        tailscale serve --bg [--https=<listen_port>] https+insecure://localhost:{port}

    When ``funnel=True``::

        tailscale funnel --bg [--https=<listen_port>] https+insecure://localhost:{port}

    Parameters
    ----------
    port:
        The local TCP port the gateway is listening on.
    listen_port:
        The Tailscale HTTPS port to expose on the tailnet. Defaults to 443
        (the primary ``/`` mapping). Pass a non-443 value to create a
        secondary HTTPS endpoint, e.g. ``listen_port=18792`` results in
        ``https://<machine>.ts.net:18792/``.
    funnel:
        If True, use ``tailscale funnel`` to make the endpoint reachable from
        the public internet (requires funnel to be enabled on the tailnet).
        Default False — serve only to tailnet peers.

    Returns
    -------
    bool
        True if the command exited with status 0, False otherwise.
    """
    if not _tailscale_available():
        logger.warning(
            "tailscale not found in PATH — skipping Tailscale serve setup"
        )
        return False

    cmd_name = "funnel" if funnel else "serve"
    cmd = [_TAILSCALE_BIN, cmd_name, "--bg"]
    if listen_port is not None and listen_port != 443:
        cmd.append(f"--https={listen_port}")
    cmd.append(f"https+insecure://localhost:{port}")

    logger.debug("Running: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.debug("tailscale %s: %s", cmd_name, stdout.decode().strip())
            return True
        logger.warning(
            "tailscale %s exited %d: %s",
            cmd_name,
            proc.returncode,
            stderr.decode().strip(),
        )
        return False
    except OSError as exc:
        logger.warning("Could not run tailscale %s: %s", cmd_name, exc)
        return False


async def remove_tailscale_serve(port: int, *, listen_port: int | None = None) -> bool:
    """Remove the Tailscale serve mapping for ``localhost:{port}``.

    Runs::

        tailscale serve --remove [--https=<listen_port>] localhost:{port}

    Parameters
    ----------
    port:
        The local TCP port to un-map.
    listen_port:
        The Tailscale HTTPS port the mapping was registered on. Must match
        the value passed to :func:`configure_tailscale_serve`.

    Returns
    -------
    bool
        True if the command exited with status 0, False otherwise.
    """
    if not _tailscale_available():
        return False

    cmd = [_TAILSCALE_BIN, "serve", "--remove"]
    if listen_port is not None and listen_port != 443:
        cmd.append(f"--https={listen_port}")
    cmd.append(f"localhost:{port}")
    logger.debug("Running: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.debug("tailscale serve --remove: %s", stdout.decode().strip())
            return True
        logger.warning(
            "tailscale serve --remove exited %d: %s",
            proc.returncode,
            stderr.decode().strip(),
        )
        return False
    except OSError as exc:
        logger.warning("Could not run tailscale serve --remove: %s", exc)
        return False


def get_tailscale_status() -> dict[str, Any]:
    """Return parsed output of ``tailscale status --json``.

    Returns an empty dict if tailscale is unavailable or the command fails.
    This is a synchronous call; call it from a thread or at startup before
    the event loop is busy.
    """
    if not _tailscale_available():
        logger.debug("tailscale not in PATH — get_tailscale_status returning {}")
        return {}

    import subprocess

    try:
        result = subprocess.run(  # noqa: S603
            [_TAILSCALE_BIN, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.debug(
                "tailscale status --json exited %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
            return {}
        return dict(json.loads(result.stdout))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.debug("get_tailscale_status failed: %s", exc)
        return {}


def detect_tailscale_url() -> str | None:
    """Detect the machine's Tailscale HTTPS base URL from ``tailscale status``.

    Returns the URL like ``https://<machine>.<tailnet>.ts.net`` if the
    machine has a Tailscale DNS name, else ``None``.
    """
    status = get_tailscale_status()
    if not status:
        return None

    # status["Self"]["DNSName"] → "mymachine.tail91e620.ts.net."  (trailing dot)
    self_raw = status.get("Self")
    if not isinstance(self_raw, dict):
        return None
    self_info: dict[str, Any] = cast("dict[str, Any]", self_raw)

    raw_name = self_info.get("DNSName", "")
    if not isinstance(raw_name, str) or not raw_name:
        return None

    # Strip trailing dot that tailscale appends
    dns_name: str = raw_name.rstrip(".")
    return f"https://{dns_name}"


__all__ = [
    "configure_tailscale_serve",
    "remove_tailscale_serve",
    "get_tailscale_status",
    "detect_tailscale_url",
]

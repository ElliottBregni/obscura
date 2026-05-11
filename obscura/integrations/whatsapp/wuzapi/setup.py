"""WhatsApp linking flow: create the wuzapi user, fetch QR, poll until linked.

This is the post-install, pre-message-flow step. Once :mod:`install` has
the binary running, ``ensure_user()`` creates the per-user slot and
``link_session()`` walks the human through scanning the QR.

The QR is saved as a PNG to ``~/.obscura/wuzapi/last-qr.png`` and (on
macOS) opened in Preview. ASCII rendering is intentionally not included
here — that would pull in ``qrcode``/``pyzbar``/``Pillow`` as runtime
deps for a one-shot setup step. If you need an ASCII QR for an SSH
session, use a separate tool (``qrencode -t ANSI`` on the PNG, or open
the PNG via ``imgcat``).

No global state; all functions take an explicit :class:`WuzapiAdminClient`
and :class:`WuzapiClient` so they're easy to mock in tests.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import platform
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from obscura.integrations.whatsapp.wuzapi.client import (
    WuzapiAdminClient,
    WuzapiClient,
)
from obscura.integrations.whatsapp.wuzapi.lifecycle import WUZAPI_HOME
from obscura.integrations.whatsapp.wuzapi.models import (
    WuzapiConnectRequest,
    WuzapiCreateUserRequest,
    WuzapiSessionStatus,
    WuzapiUser,
)

logger = logging.getLogger(__name__)

DEFAULT_USER_NAME: Final[str] = "obscura"
USER_TOKEN_FILE: Final[Path] = WUZAPI_HOME / "user.token"
ADMIN_TOKEN_FILE: Final[Path] = WUZAPI_HOME / "admin.token"
LAST_QR_PNG: Final[Path] = WUZAPI_HOME / "last-qr.png"


# ---------------------------------------------------------------------------
# User creation / lookup
# ---------------------------------------------------------------------------


async def ensure_user(
    admin: WuzapiAdminClient,
    *,
    name: str = DEFAULT_USER_NAME,
    events: list[str] | None = None,
) -> WuzapiUser:
    """Return the wuzapi user named ``name``, creating it if absent.

    Persists the user token to ``~/.obscura/wuzapi/user.token`` (mode 600)
    so subsequent operations can recover it without round-tripping admin.
    """
    import secrets as _secrets

    existing = await admin.list_users()
    for u in existing:
        if u.name == name:
            USER_TOKEN_FILE.write_text(u.token)
            USER_TOKEN_FILE.chmod(0o600)
            return u

    token = _secrets.token_urlsafe(24)
    req = WuzapiCreateUserRequest(
        name=name,
        token=token,
        events=",".join(events or ["Message"]),
    )
    user = await admin.create_user(req)
    USER_TOKEN_FILE.write_text(user.token)
    USER_TOKEN_FILE.chmod(0o600)
    return user


def load_user_token() -> str:
    """Read the persisted user token, raising on absence."""
    if not USER_TOKEN_FILE.is_file():
        raise RuntimeError(
            f"user token not found at {USER_TOKEN_FILE}; run setup_user first"
        )
    return USER_TOKEN_FILE.read_text().strip()


def load_admin_token() -> str:
    """Read the admin token from the install step."""
    if not ADMIN_TOKEN_FILE.is_file():
        raise RuntimeError(
            f"admin token not found at {ADMIN_TOKEN_FILE}; run install first"
        )
    return ADMIN_TOKEN_FILE.read_text().strip()


# ---------------------------------------------------------------------------
# QR rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QRArtifacts:
    """Where the QR was rendered. ``ascii_text`` is always ``None`` today.

    The field is retained for callers that want to print the QR inline —
    they can supply their own rendering via ``qrencode -t ANSI`` on
    ``png_path`` if needed.
    """

    png_path: Path
    ascii_text: str | None


def _save_qr_png(qr_data_url: str) -> Path:
    """Decode a ``data:image/png;base64,...`` URL into a file."""
    if not qr_data_url.startswith("data:image/png;base64,"):
        raise ValueError(f"unexpected QR data URL: {qr_data_url[:40]!r}")
    png_bytes = base64.b64decode(qr_data_url.split(",", 1)[1])
    LAST_QR_PNG.parent.mkdir(parents=True, exist_ok=True)
    LAST_QR_PNG.write_bytes(png_bytes)
    return LAST_QR_PNG


def render_qr(qr_data_url: str) -> QRArtifacts:
    """Render the QR to disk (PNG). ``ascii_text`` is left ``None``."""
    png_path = _save_qr_png(qr_data_url)
    return QRArtifacts(png_path=png_path, ascii_text=None)


def open_qr_in_preview(png_path: Path) -> None:
    """On macOS, hand the PNG to the default image viewer (Preview)."""
    if platform.system() != "Darwin":
        return
    subprocess.run(["open", str(png_path)], check=False)


# ---------------------------------------------------------------------------
# Link orchestration
# ---------------------------------------------------------------------------


async def link_session(
    client: WuzapiClient,
    *,
    on_qr: Callable[[QRArtifacts], Awaitable[None]] | None = None,
    poll_interval_s: float = 2.0,
    timeout_s: float = 180.0,
) -> WuzapiSessionStatus:
    """Link the WhatsApp account: connect → present QR → poll → return status.

    If the session is already linked, returns immediately without showing
    a QR. ``on_qr`` is invoked once with the rendered QR artifacts if a
    fresh scan is needed — defaults to opening Preview on macOS.

    Raises :class:`TimeoutError` if the user doesn't scan within
    ``timeout_s`` (default 3 minutes).
    """
    status = await client.session_status()
    if status.logged_in:
        return status

    # Ensure we have an active websocket to WhatsApp (idempotent connect)
    try:
        await client.connect(WuzapiConnectRequest())
    except Exception:
        # "already connected" is the common case — fine
        pass

    # Wait briefly for wuzapi to generate the QR
    await asyncio.sleep(1.0)
    qr_resp = await client.get_qr()
    if not qr_resp.qr_code:
        # Session might have linked between our status check and now
        status = await client.session_status()
        if status.logged_in:
            return status
        raise RuntimeError("wuzapi returned empty QR — try again")

    artifacts = render_qr(qr_resp.qr_code)
    if on_qr is not None:
        await on_qr(artifacts)
    else:
        open_qr_in_preview(artifacts.png_path)

    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        status = await client.session_status()
        if status.logged_in:
            return status
        await asyncio.sleep(poll_interval_s)
    raise TimeoutError(f"WhatsApp not linked within {timeout_s}s")


__all__ = [
    "ADMIN_TOKEN_FILE",
    "DEFAULT_USER_NAME",
    "LAST_QR_PNG",
    "QRArtifacts",
    "USER_TOKEN_FILE",
    "ensure_user",
    "link_session",
    "load_admin_token",
    "load_user_token",
    "open_qr_in_preview",
    "render_qr",
]

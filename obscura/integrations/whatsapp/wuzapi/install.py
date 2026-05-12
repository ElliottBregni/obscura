"""First-time install orchestration for the wuzapi sidecar.

Pipeline (each step is idempotent — safe to re-run):

1. Verify prerequisites (``git``, ``go``)
2. Clone or pull ``asternic/wuzapi`` into ``~/.obscura/wuzapi/src``
3. Build the binary into ``~/.obscura/wuzapi/wuzapi``
4. Initialize state directory + log directory
5. Generate secrets (admin token, HMAC key, encryption key) — only on first install
6. Write ``.env`` with secret env vars
7. Render and install the LaunchAgent plist

This module does NOT do user creation, QR linking, or webhook config —
those are in :mod:`setup`. Install puts the infrastructure in place;
setup makes WhatsApp actually flow.

All side effects target ``~/.obscura/wuzapi/`` and ``~/Library/LaunchAgents/``.
No system-wide changes.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from obscura.integrations.whatsapp.wuzapi.lifecycle import (
    LABEL,
    PLIST_PATH,
    WUZAPI_HOME,
    LifecycleError,
)

logger = logging.getLogger(__name__)

WUZAPI_REPO_URL: Final[str] = "https://github.com/asternic/wuzapi.git"
WUZAPI_LOG_DIR: Final[Path] = Path.home() / ".obscura" / "logs"


# ---------------------------------------------------------------------------
# Errors + result types
# ---------------------------------------------------------------------------


class InstallError(RuntimeError):
    """Surface for any install-pipeline failure."""


@dataclass(frozen=True)
class InstallReport:
    """Per-step outcome of an install run."""

    cloned: bool  # True if we cloned fresh (False = already present)
    built: bool  # True if we rebuilt (False = binary already up to date)
    plist_written: bool
    secrets_generated: bool


# ---------------------------------------------------------------------------
# Step 1 — prerequisites
# ---------------------------------------------------------------------------


def _which_or_raise(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise InstallError(
            f"required tool {binary!r} not found on PATH; install it first"
        )
    return path


def check_prerequisites() -> None:
    """Verify ``git`` and ``go`` are available. Raises :class:`InstallError`."""
    _which_or_raise("git")
    _which_or_raise("go")


# ---------------------------------------------------------------------------
# Step 2 — clone / pull
# ---------------------------------------------------------------------------


def _src_dir() -> Path:
    return WUZAPI_HOME / "src"


def clone_or_pull(*, repo_url: str = WUZAPI_REPO_URL) -> bool:
    """Clone wuzapi if absent; pull --ff-only if present. Returns True on clone."""
    src = _src_dir()
    if (src / ".git").is_dir():
        result = subprocess.run(
            ["git", "-C", str(src), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("git pull failed (continuing): %s", result.stderr.strip())
        return False
    src.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(src)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise InstallError(f"git clone failed: {result.stderr.strip()}")
    return True


# ---------------------------------------------------------------------------
# Step 3 — build
# ---------------------------------------------------------------------------


def _binary_path() -> Path:
    return WUZAPI_HOME / "wuzapi"


def build_binary(*, force: bool = False) -> bool:
    """Compile the wuzapi Go binary. Returns True if rebuilt.

    Skips if the binary already exists and ``force`` is False — wuzapi
    builds in ~15s but skipping is still nicer on re-runs.
    """
    binary = _binary_path()
    if binary.is_file() and not force:
        return False
    src = _src_dir()
    if not (src / "go.mod").is_file():
        raise InstallError(f"wuzapi source missing at {src}; clone first")
    result = subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=str(src),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise InstallError(f"go build failed: {result.stderr.strip()}")
    binary.chmod(0o755)
    return True


# ---------------------------------------------------------------------------
# Step 4 — state + log directories
# ---------------------------------------------------------------------------


def init_directories() -> None:
    """Create ``~/.obscura/wuzapi/state`` and ``~/.obscura/logs`` if missing."""
    (WUZAPI_HOME / "state").mkdir(parents=True, exist_ok=True)
    WUZAPI_LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Step 5 — secrets
# ---------------------------------------------------------------------------


def generate_secrets(*, force: bool = False) -> bool:
    """Generate admin/HMAC/encryption secrets if they don't exist.

    Returns True if new secrets were generated (i.e. files were created).
    Each file is written with mode 600.
    """
    targets = {
        "admin.token": secrets.token_hex(32),
        "hmac.key": secrets.token_urlsafe(24),
        "encryption.key": secrets.token_urlsafe(24),
    }
    generated = False
    for filename, value in targets.items():
        path = WUZAPI_HOME / filename
        if path.is_file() and not force:
            continue
        path.write_text(value)
        path.chmod(0o600)
        generated = True
    return generated


def write_env_file() -> Path:
    """Render ``~/.obscura/wuzapi/.env`` from the secret files. Returns the path."""
    admin = (WUZAPI_HOME / "admin.token").read_text().strip()
    hmac_key = (WUZAPI_HOME / "hmac.key").read_text().strip()
    enc_key = (WUZAPI_HOME / "encryption.key").read_text().strip()
    env_path = WUZAPI_HOME / ".env"
    env_path.write_text(
        f"WUZAPI_ADMIN_TOKEN={admin}\n"
        f"WUZAPI_GLOBAL_HMAC_KEY={hmac_key}\n"
        f"WUZAPI_GLOBAL_ENCRYPTION_KEY={enc_key}\n"
    )
    env_path.chmod(0o600)
    return env_path


# ---------------------------------------------------------------------------
# Step 6 — LaunchAgent plist
# ---------------------------------------------------------------------------


_PLIST_TEMPLATE: Final[str] = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{label}</string>

    <key>Comment</key>
    <string>Obscura WhatsApp sidecar (wuzapi REST wrapper for whatsmeow)</string>

    <key>ProgramArguments</key>
    <array>
      <string>{binary}</string>
      <string>-address</string>
      <string>127.0.0.1</string>
      <string>-port</string>
      <string>{port}</string>
      <string>-datadir</string>
      <string>./state</string>
      <string>-logtype</string>
      <string>json</string>
      <string>-skipmedia</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{home}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>5</integer>

    <key>StandardOutPath</key>
    <string>{out_log}</string>

    <key>StandardErrorPath</key>
    <string>{err_log}</string>
  </dict>
</plist>
"""


def write_plist(*, port: int = 18793) -> bool:
    """Render and install ``dev.obscura.wuzapi.plist``. Returns True if written."""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = _PLIST_TEMPLATE.format(
        label=LABEL,
        binary=str(_binary_path()),
        port=port,
        home=str(WUZAPI_HOME),
        out_log=str(WUZAPI_LOG_DIR / "wuzapi.log"),
        err_log=str(WUZAPI_LOG_DIR / "wuzapi.err.log"),
    )
    existing = PLIST_PATH.read_text() if PLIST_PATH.is_file() else None
    if existing == plist:
        return False
    PLIST_PATH.write_text(plist)
    return True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def install(*, port: int = 18793, force_rebuild: bool = False) -> InstallReport:
    """Run the full install pipeline. Idempotent.

    :param port: Loopback port for the wuzapi REST API. Default 18793.
    :param force_rebuild: Rebuild the Go binary even if it exists.

    Caller is responsible for calling :func:`lifecycle.load` and
    :func:`lifecycle.kickstart` after this returns, then handing off to
    :mod:`setup` for user creation + QR linking.
    """
    check_prerequisites()
    WUZAPI_HOME.mkdir(parents=True, exist_ok=True)
    cloned = clone_or_pull()
    built = build_binary(force=force_rebuild or cloned)
    init_directories()
    secrets_generated = generate_secrets()
    write_env_file()
    plist_written = write_plist(port=port)
    return InstallReport(
        cloned=cloned,
        built=built,
        plist_written=plist_written,
        secrets_generated=secrets_generated,
    )


def uninstall(*, wipe_state: bool = False) -> None:
    """Stop and remove the LaunchAgent. Optionally wipe state.

    Wiping state deletes the WhatsApp session DB — you'd have to re-scan
    QR after a fresh install. We default to preserving state for that
    reason.
    """
    from obscura.integrations.whatsapp.wuzapi import lifecycle

    try:
        lifecycle.unload()
    except LifecycleError:
        pass
    if PLIST_PATH.is_file():
        PLIST_PATH.unlink()
    if wipe_state and WUZAPI_HOME.is_dir():
        shutil.rmtree(WUZAPI_HOME)


__all__ = [
    "InstallError",
    "InstallReport",
    "WUZAPI_REPO_URL",
    "build_binary",
    "check_prerequisites",
    "clone_or_pull",
    "generate_secrets",
    "init_directories",
    "install",
    "uninstall",
    "write_env_file",
    "write_plist",
]

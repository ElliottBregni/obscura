"""Sandbox & access-control namespace for system tools.

Pure-policy: no side effects, no tool registrations. Imported by every
``_<group>.py`` submodule under ``obscura.tools.system`` so they share a single
source of truth for sandbox decisions.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib import parse as url_parse

from obscura.core.paths import resolve_obscura_home, resolve_obscura_output_dir
import logging

logger = logging.getLogger(__name__)


class Policy:
    """Path / URL / command access-control namespace.

    All members are static or class methods — ``Policy`` is never instantiated;
    it exists purely as a namespace that strict pyright can read cleanly.
    """

    DEFAULT_DENIED_COMMANDS: ClassVar[tuple[str, ...]] = (
        "rm",
        "sudo",
        "shutdown",
        "reboot",
        "diskutil",
        "mkfs",
        "dd",
    )

    # SSRF guard: blocked private/loopback/link-local nets. Set
    # OBSCURA_ALLOW_PRIVATE_URLS=true to bypass (dev/test only).
    BLOCKED_NETWORKS: ClassVar[
        tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    ] = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fd00::/8"),
        ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    )

    # Mutable: extra dirs registered via add_allowed_dir() at runtime.
    runtime_allowed_dirs: ClassVar[list[Path]] = []

    # ------------------------------------------------------------------
    # Env helpers
    # ------------------------------------------------------------------

    @staticmethod
    def env_flag(name: str, default: bool = False) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def unsafe_full_access_enabled() -> bool:
        return Policy.env_flag(
            "OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS",
            default=False,
        )

    @staticmethod
    def normalize_list(values: str) -> set[str]:
        return {part.strip() for part in values.split(",") if part.strip()}

    @staticmethod
    def string_key_dict(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        mapping = cast("dict[Any, Any]", value)
        return {str(key): item for key, item in mapping.items()}

    # ------------------------------------------------------------------
    # Path resolution & sandbox
    # ------------------------------------------------------------------

    @classmethod
    def add_allowed_dir(cls, path: str | Path) -> None:
        """Register an additional directory as allowed at runtime.

        Bypasses the OBSCURA_SYSTEM_TOOLS_BASE_DIR check. Useful for
        dynamically granting access without restarting the process.
        """
        resolved = Path(path).expanduser().resolve()
        if resolved not in cls.runtime_allowed_dirs:
            cls.runtime_allowed_dirs.append(resolved)

    @staticmethod
    def resolve_base_dir() -> Path | None:
        raw = os.environ.get("OBSCURA_SYSTEM_TOOLS_BASE_DIR", "").strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    @staticmethod
    def is_cwd_allowed(cwd: str) -> bool:
        base = Policy.resolve_base_dir()
        if base is None:
            return True
        if not cwd:
            return True
        candidate = Path(cwd).expanduser().resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            logger.debug("suppressed exception in is_cwd_allowed", exc_info=True)
            return False
        return True

    @staticmethod
    def resolve_path(path: str) -> Path:
        """Resolve a tool-provided file path.

        Absolute paths and ``~/`` paths are used as-is. Relative paths are
        resolved against ``~/.obscura/output/`` (NOT the working directory)
        so agent-generated files land in the Obscura data dir instead of
        polluting the project tree.

        Set ``OBSCURA_TOOLS_RELATIVE_TO_CWD=1`` to restore the old cwd
        behaviour when explicitly desired.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            if os.environ.get("OBSCURA_TOOLS_RELATIVE_TO_CWD", "").lower() in (
                "1",
                "true",
                "yes",
            ):
                candidate = Path.cwd() / candidate
            else:
                candidate = resolve_obscura_output_dir() / candidate
        return candidate.resolve()

    @classmethod
    def is_path_allowed(cls, path: Path) -> bool:
        base = Policy.resolve_base_dir()
        if base is None:
            return True

        # 1. Runtime-registered dirs.
        for allowed in cls.runtime_allowed_dirs:
            try:
                path.relative_to(allowed)
                return True
            except ValueError:
                logger.debug("suppressed exception in is_path_allowed", exc_info=True)

        # 2. Always allow .obscura/ — agent-owned data must never be
        #    locked out by a project-scoped base-dir restriction.
        resolved = path.resolve()
        for part in resolved.parts:
            if part == ".obscura":
                return True
        try:
            obscura_home = resolve_obscura_home().resolve()
            try:
                resolved.relative_to(obscura_home)
                return True
            except ValueError:
                logger.debug("suppressed exception in is_path_allowed", exc_info=True)
        except Exception:
            logger.debug("suppressed exception in is_path_allowed", exc_info=True)

        # 3. Standard base-dir check.
        try:
            path.relative_to(base)
        except ValueError:
            logger.debug("suppressed exception in is_path_allowed", exc_info=True)
            return False
        return True

    @staticmethod
    def is_vault_write_allowed(path: Path) -> bool:
        """Return False if path is inside vault/user/ or vault/shared/.

        ``vault/agent/`` is the only zone agents may write to. Paths
        outside the vault are unaffected and always return True.
        """
        try:
            vault_root = resolve_obscura_home() / "vault"
            rel = path.resolve().relative_to(vault_root.resolve())
            zone = rel.parts[0] if rel.parts else ""
            if zone in ("user", "shared"):
                return False
        except (ValueError, Exception):
            logger.debug(
                "suppressed exception in is_vault_write_allowed", exc_info=True
            )
        return True

    # ------------------------------------------------------------------
    # URL validation (SSRF guard)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_url(url: str) -> str:
        """Validate a URL against SSRF attacks.

        - Only ``http://`` and ``https://`` schemes allowed.
        - DNS is resolved pre-flight; private/internal IPs are blocked.
        - Set ``OBSCURA_ALLOW_PRIVATE_URLS=true`` to bypass (dev/test only).

        Returns the validated URL string. Raises ``ValueError`` on
        blocked URLs.
        """
        if Policy.env_flag("OBSCURA_ALLOW_PRIVATE_URLS", default=False):
            return url

        parsed = url_parse.urlparse(url)

        if parsed.scheme not in ("http", "https"):
            msg = (
                f"URL scheme {parsed.scheme!r} is not allowed. "
                "Only http:// and https:// URLs are permitted."
            )
            raise ValueError(msg)

        hostname = parsed.hostname
        if not hostname:
            msg = "URL has no hostname."
            raise ValueError(msg)

        try:
            addrinfos = socket.getaddrinfo(
                hostname,
                parsed.port or 443,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            msg = f"DNS resolution failed for {hostname!r}: {exc}"
            raise ValueError(msg) from exc

        for _family, _type, _proto, _canonname, sockaddr in addrinfos:
            ip_str = cast("str", sockaddr[0])
            addr = ipaddress.ip_address(ip_str)
            for net in Policy.BLOCKED_NETWORKS:
                if addr in net:
                    msg = (
                        f"URL {url!r} resolves to private/internal address "
                        f"{ip_str} (in {net}). Request blocked to prevent SSRF. "
                        "Set OBSCURA_ALLOW_PRIVATE_URLS=true to override."
                    )
                    raise ValueError(msg)

        return url

    # ------------------------------------------------------------------
    # Error formatting
    # ------------------------------------------------------------------

    @staticmethod
    def json_error(error: str, **extra: object) -> str:
        payload: dict[str, object] = {"ok": False, "error": error, "exit_code": -1}
        payload.update(extra)
        if error == "path_not_allowed" and "hint" not in payload:
            base = Policy.resolve_base_dir()
            payload["hint"] = (
                f"Path is outside Obscura's sandbox "
                f"(OBSCURA_SYSTEM_TOOLS_BASE_DIR={base}). "
                "Unset the env var or widen the base dir to grant access."
                if base is not None
                else "Path rejected by Obscura's sandbox policy."
            )
        return json.dumps(payload)

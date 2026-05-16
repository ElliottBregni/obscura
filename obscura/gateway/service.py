"""Service installation and management for Obscura Gateway.

Supports:
- macOS LaunchAgent (user and system)
- Linux systemd (user and system)
- Windows Service (via nssm or similar)
- Docker container
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Literal

from obscura.gateway.config import GatewayConfig, GatewayMode

logger = logging.getLogger(__name__)


class ServiceInstaller:
    """Install and manage Obscura Gateway as a system service."""

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig.from_env()
        self.system = platform.system()

    def install(
        self,
        mode: Literal["user", "system"] = "user",
        start: bool = True,
    ) -> bool:
        """Install the gateway service.

        Args:
            mode: "user" for current user only, "system" for all users
            start: Whether to start the service immediately

        Returns:
            True if installation succeeded
        """
        if self.system == "Darwin":
            return self._install_macos(mode, start)
        elif self.system == "Linux":
            return self._install_linux(mode, start)
        else:
            logger.error(f"Service installation not supported on {self.system}")
            return False

    def uninstall(self, mode: Literal["user", "system"] = "user") -> bool:
        """Uninstall the gateway service."""
        if self.system == "Darwin":
            return self._uninstall_macos(mode)
        elif self.system == "Linux":
            return self._uninstall_linux(mode)
        return False

    def start(self, mode: Literal["user", "system"] = "user") -> bool:
        """Start the service."""
        try:
            if self.system == "Darwin":
                label = self._get_launchd_label(mode)
                subprocess.run(["launchctl", "start", label], check=True)
            elif self.system == "Linux":
                service_name = self._get_systemd_name(mode)
                cmd = ["systemctl", "--user"] if mode == "user" else ["systemctl"]
                subprocess.run([*cmd, "start", service_name], check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to start service: {e}")
            return False

    def stop(self, mode: Literal["user", "system"] = "user") -> bool:
        """Stop the service."""
        try:
            if self.system == "Darwin":
                label = self._get_launchd_label(mode)
                subprocess.run(["launchctl", "stop", label], check=True)
            elif self.system == "Linux":
                service_name = self._get_systemd_name(mode)
                cmd = ["systemctl", "--user"] if mode == "user" else ["systemctl"]
                subprocess.run([*cmd, "stop", service_name], check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stop service: {e}")
            return False

    def status(self, mode: Literal["user", "system"] = "user") -> dict:
        """Get service status."""
        status = {"installed": False, "running": False, "enabled": False}

        try:
            if self.system == "Darwin":
                label = self._get_launchd_label(mode)
                result = subprocess.run(
                    ["launchctl", "list", label], capture_output=True, text=True
                )
                status["installed"] = result.returncode == 0
                status["running"] = "PID" in result.stdout

            elif self.system == "Linux":
                service_name = self._get_systemd_name(mode)
                cmd = ["systemctl", "--user"] if mode == "user" else ["systemctl"]

                # Check if installed
                result = subprocess.run(
                    [*cmd, "cat", service_name], capture_output=True
                )
                status["installed"] = result.returncode == 0

                # Check if running
                result = subprocess.run(
                    [*cmd, "is-active", service_name], capture_output=True
                )
                status["running"] = result.returncode == 0

                # Check if enabled
                result = subprocess.run(
                    [*cmd, "is-enabled", service_name], capture_output=True
                )
                status["enabled"] = result.returncode == 0

        except Exception as e:
            logger.error(f"Failed to get status: {e}")

        return status

    def _install_macos(self, mode: str, start: bool) -> bool:
        """Install macOS LaunchAgent/LaunchDaemon."""
        plist_content = self._generate_launchd_plist(mode)
        plist_path = self._get_launchd_path(mode)

        # Create directory if needed
        plist_path.parent.mkdir(parents=True, exist_ok=True)

        # Write plist file
        plist_path.write_text(plist_content)

        # Load the service
        try:
            label = self._get_launchd_label(mode)
            subprocess.run(["launchctl", "load", str(plist_path)], check=True)

            if start:
                subprocess.run(["launchctl", "start", label], check=True)

            logger.info(f"Installed LaunchAgent at {plist_path}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to load LaunchAgent: {e}")
            return False

    def _uninstall_macos(self, mode: str) -> bool:
        """Uninstall macOS LaunchAgent/LaunchDaemon."""
        try:
            label = self._get_launchd_label(mode)
            plist_path = self._get_launchd_path(mode)

            # Stop and unload
            subprocess.run(["launchctl", "stop", label], capture_output=True)
            subprocess.run(
                ["launchctl", "unload", str(plist_path)], capture_output=True
            )

            # Remove plist
            if plist_path.exists():
                plist_path.unlink()

            logger.info(f"Uninstalled LaunchAgent {label}")
            return True

        except Exception as e:
            logger.error(f"Failed to uninstall: {e}")
            return False

    def _install_linux(self, mode: str, start: bool) -> bool:
        """Install Linux systemd service."""
        service_content = self._generate_systemd_service(mode)
        service_path = self._get_systemd_path(mode)

        # Create directory if needed
        service_path.parent.mkdir(parents=True, exist_ok=True)

        # Write service file
        service_path.write_text(service_content)

        try:
            service_name = self._get_systemd_name(mode)
            cmd = ["systemctl", "--user"] if mode == "user" else ["systemctl"]

            # Reload systemd
            subprocess.run([*cmd, "daemon-reload"], check=True)

            # Enable service
            subprocess.run([*cmd, "enable", service_name], check=True)

            if start:
                subprocess.run([*cmd, "start", service_name], check=True)

            logger.info(f"Installed systemd service at {service_path}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to install systemd service: {e}")
            return False

    def _uninstall_linux(self, mode: str) -> bool:
        """Uninstall Linux systemd service."""
        try:
            service_name = self._get_systemd_name(mode)
            service_path = self._get_systemd_path(mode)
            cmd = ["systemctl", "--user"] if mode == "user" else ["systemctl"]

            # Stop and disable
            subprocess.run([*cmd, "stop", service_name], capture_output=True)
            subprocess.run([*cmd, "disable", service_name], capture_output=True)

            # Remove service file
            if service_path.exists():
                service_path.unlink()

            # Reload systemd
            subprocess.run([*cmd, "daemon-reload"], check=True)

            logger.info(f"Uninstalled systemd service {service_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to uninstall: {e}")
            return False

    def _generate_launchd_plist(self, mode: str) -> str:
        """Generate macOS LaunchAgent plist content."""
        label = self._get_launchd_label(mode)
        obscura_path = Path.home() / "dev" / "obscura-main"

        env_vars = {
            "OBSCURA_GATEWAY_MODE": self.config.mode.name.lower(),
            "OBSCURA_GATEWAY_PORT": str(self.config.native.port),
            "OBSCURA_GATEWAY_HOST": self.config.native.host,
            "OBSCURA_OPENCLAW_ENABLED": str(self.config.openclaw.enabled).lower(),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        }

        env_xml = "\n".join(
            f"        <key>{k}</key>\n        <string>{v}</string>"
            for k, v in env_vars.items()
        )

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{obscura_path}/.venv/bin/python</string>
        <string>-m</string>
        <string>obscura.gateway</string>
        <string>--mode</string>
        <string>{self.config.mode.name.lower()}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
    <key>WorkingDirectory</key>
    <string>{obscura_path}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.obscura/logs/gateway.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.obscura/logs/gateway.error.log</string>
</dict>
</plist>"""

    def _generate_systemd_service(self, mode: str) -> str:
        """Generate Linux systemd service content."""
        service_name = self._get_systemd_name(mode)
        obscura_path = Path.home() / "dev" / "obscura-main"

        env_vars = " ".join(
            [
                f"OBSCURA_GATEWAY_MODE={self.config.mode.name.lower()}",
                f"OBSCURA_GATEWAY_PORT={self.config.native.port}",
                f"OBSCURA_GATEWAY_HOST={self.config.native.host}",
                f"OBSCURA_OPENCLAW_ENABLED={str(self.config.openclaw.enabled).lower()}",
            ]
        )

        return f"""[Unit]
Description=Obscura Gateway ({self.config.mode.name} mode)
After=network.target

[Service]
Type=simple
User={os.getenv("USER") if mode == "user" else "obscura"}
WorkingDirectory={obscura_path}
Environment={env_vars}
ExecStart={obscura_path}/.venv/bin/python -m obscura.gateway --mode {self.config.mode.name.lower()}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy={"default.target" if mode == "user" else "multi-user.target"}
"""

    def _get_launchd_label(self, mode: str) -> str:
        """Get LaunchAgent/Daemon label."""
        return f"ai.obscura.gateway.{mode}"

    def _get_launchd_path(self, mode: str) -> Path:
        """Get LaunchAgent/Daemon plist path."""
        label = self._get_launchd_label(mode)
        if mode == "user":
            return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        else:
            return Path("/Library/LaunchDaemons") / f"{label}.plist"

    def _get_systemd_name(self, mode: str) -> str:
        """Get systemd service name."""
        return f"obscura-gateway-{mode}.service"

    def _get_systemd_path(self, mode: str) -> Path:
        """Get systemd service file path."""
        name = self._get_systemd_name(mode)
        if mode == "user":
            return Path.home() / ".config" / "systemd" / "user" / name
        else:
            return Path("/etc/systemd/system") / name


def main() -> int:
    """CLI entry point for service management."""
    parser = argparse.ArgumentParser(description="Obscura Gateway Service Manager")
    parser.add_argument(
        "action",
        choices=["install", "uninstall", "start", "stop", "status", "restart"],
        help="Action to perform",
    )
    parser.add_argument(
        "--mode",
        choices=["user", "system"],
        default="user",
        help="Installation mode (user or system)",
    )
    parser.add_argument(
        "--gateway-mode",
        choices=["auto", "openclaw", "native", "mcp", "hybrid"],
        default="auto",
        help="Gateway operational mode",
    )
    parser.add_argument("--port", type=int, default=18790, help="Gateway port")
    parser.add_argument(
        "--no-start", action="store_true", help="Don't start service after install"
    )

    args = parser.parse_args()

    # Create config from args
    config = GatewayConfig.from_env()
    config.mode = GatewayMode[args.gateway_mode.upper()]
    config.native.port = args.port

    installer = ServiceInstaller(config)

    if args.action == "install":
        success = installer.install(mode=args.mode, start=not args.no_start)

    elif args.action == "uninstall":
        success = installer.uninstall(mode=args.mode)

    elif args.action == "start":
        success = installer.start(mode=args.mode)

    elif args.action == "stop":
        success = installer.stop(mode=args.mode)

    elif args.action == "restart":
        installer.stop(mode=args.mode)
        success = installer.start(mode=args.mode)

    elif args.action == "status":
        status = installer.status(mode=args.mode)
        print(f"Service Status ({args.mode} mode):")
        print(f"  Installed: {status['installed']}")
        print(f"  Running: {status['running']}")
        print(f"  Enabled: {status.get('enabled', 'N/A')}")
        return 0

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

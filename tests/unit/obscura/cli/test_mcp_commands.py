"""Tests for obscura.cli.mcp_commands — MCP server management CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from obscura.cli.mcp_commands import (
    MCP_COMMANDS,
    cmd_mcp_discover,
    cmd_mcp_env,
    cmd_mcp_install,
    cmd_mcp_list,
    cmd_mcp_select,
    handle_mcp_command,
)
from obscura.integrations.mcp.catalog import MCPCatalogEntry
from obscura.integrations.mcp.config_loader import DiscoveredMCPServer


@pytest.fixture
def mock_catalog_entries() -> list[MCPCatalogEntry]:
    """Sample MCP catalog entries."""
    return [
        MCPCatalogEntry(name="GitHub", slug="gh", url="https://github.com/gh", rank=1),
        MCPCatalogEntry(name="Postgres", slug="pg", url="https://github.com/pg", rank=2),
    ]


@pytest.fixture  
def mock_discovered_servers() -> list[DiscoveredMCPServer]:
    """Sample discovered servers."""
    return [
        DiscoveredMCPServer(
            name="github",
            transport="stdio",
            command="npx",
            args=("-y", "gh"),
            url="",
            env={},
            tools=(),
            missing_env=(),
        ),
    ]


class TestMcpCommandsRegistry:
    """Test MCP_COMMANDS registry structure."""
    
    def test_registry_has_all_commands(self) -> None:
        """Registry contains all expected commands."""
        expected = {"discover", "list", "select", "env", "install"}
        assert set(MCP_COMMANDS.keys()) == expected
    
    def test_registry_entries_are_tuples(self) -> None:
        """Each entry is (handler, description) tuple."""
        for entry in MCP_COMMANDS.values():
            assert isinstance(entry, tuple)
            assert len(entry) == 2
            assert callable(entry[0])
            assert isinstance(entry[1], str)


class TestCmdMcpDiscover:
    """Test /mcp discover command."""

    @patch("obscura.cli.mcp_commands.MCPServersOrgCatalogProvider")
    @patch("obscura.cli.mcp_commands.console")
    def test_discover_default_limit(
        self, mock_console: Mock, mock_provider_class: Mock
    ) -> None:
        """Test discover with default limit."""
        mock_provider = Mock()
        mock_provider.fetch_top.return_value = []
        mock_provider_class.return_value = mock_provider
        
        cmd_mcp_discover([])
        
        mock_provider.fetch_top.assert_called_once_with(limit=20)

    @patch("obscura.cli.mcp_commands.MCPServersOrgCatalogProvider")
    def test_discover_custom_limit(self, mock_provider_class: Mock) -> None:
        """Test discover with custom limit."""
        mock_provider = Mock()
        mock_provider.fetch_top.return_value = []
        mock_provider_class.return_value = mock_provider
        
        cmd_mcp_discover(["--limit", "5"])
        
        mock_provider.fetch_top.assert_called_once_with(limit=5)

    @patch("obscura.cli.mcp_commands.MCPServersOrgCatalogProvider")
    @patch("obscura.cli.mcp_commands.print_error")
    def test_discover_handles_errors(
        self, mock_error: Mock, mock_provider_class: Mock
    ) -> None:
        """Test discover handles API errors."""
        mock_provider = Mock()
        mock_provider.fetch_top.side_effect = Exception("API Error")
        mock_provider_class.return_value = mock_provider
        
        cmd_mcp_discover([])
        
        mock_error.assert_called()


class TestCmdMcpList:
    """Test /mcp list command."""

    @patch("obscura.cli.mcp_commands.discover_mcp_servers")
    @patch("obscura.cli.mcp_commands.console")
    def test_list_with_servers(
        self, mock_console: Mock, mock_discover: Mock,
        mock_discovered_servers: list[DiscoveredMCPServer]
    ) -> None:
        """Test listing configured servers."""
        mock_discover.return_value = mock_discovered_servers
        
        cmd_mcp_list([])
        
        mock_discover.assert_called_once()
        assert mock_console.print.called

    @patch("obscura.cli.mcp_commands.discover_mcp_servers")
    @patch("obscura.cli.mcp_commands.print_info")
    def test_list_no_servers(self, mock_info: Mock, mock_discover: Mock) -> None:
        """Test list when no servers configured."""
        mock_discover.return_value = []
        
        cmd_mcp_list([])
        
        assert mock_info.called


class TestCmdMcpSelect:
    """Test /mcp select command."""

    @patch("obscura.cli.mcp_commands.print_error")
    def test_select_no_args(self, mock_error: Mock) -> None:
        """Test select without task description."""
        cmd_mcp_select([])
        
        mock_error.assert_called()

    @patch("obscura.cli.mcp_commands.discover_mcp_servers")
    @patch("obscura.cli.mcp_commands.select_servers_for_task")
    @patch("obscura.cli.mcp_commands.console")
    def test_select_matches(
        self, mock_console: Mock, mock_select: Mock, mock_discover: Mock
    ) -> None:
        """Test select matches servers."""
        mock_discover.return_value = []
        mock_select.return_value = ["github"]
        
        cmd_mcp_select(["github", "task"])
        
        mock_select.assert_called_once()


class TestCmdMcpEnv:
    """Test /mcp env command."""

    @patch("obscura.cli.mcp_commands.discover_mcp_servers")
    @patch("obscura.cli.mcp_commands.print_ok")
    def test_env_all_set(self, mock_ok: Mock, mock_discover: Mock) -> None:
        """Test env when all vars set."""
        servers = [
            DiscoveredMCPServer(
                name="test",
                transport="stdio",
                command="npx",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            )
        ]
        mock_discover.return_value = servers
        
        cmd_mcp_env([])
        
        mock_ok.assert_called()


class TestCmdMcpInstall:
    """Test /mcp install command."""

    @patch("obscura.cli.mcp_commands.print_error")
    def test_install_no_args(self, mock_error: Mock) -> None:
        """Test install without slug."""
        cmd_mcp_install([])
        
        mock_error.assert_called()

    @patch("obscura.cli.mcp_commands.resolve_obscura_mcp_dir")
    @patch("obscura.cli.mcp_commands.print_ok")
    def test_install_new_server(self, mock_ok: Mock, mock_resolve: Mock, tmp_path: Path) -> None:
        """Test installing new server."""
        mcp_dir = tmp_path / "mcp"
        mcp_dir.mkdir()
        mock_resolve.return_value = mcp_dir
        
        cmd_mcp_install(["test-server"])
        
        config_file = mcp_dir / "config.json"
        assert config_file.exists()


class TestHandleMcpCommand:
    """Test MCP command router."""

    @patch("obscura.cli.mcp_commands.console")
    def test_handle_no_args_shows_help(self, mock_console: Mock) -> None:
        """Test /mcp shows help."""
        handle_mcp_command([])
        
        assert mock_console.print.called

    @patch("obscura.cli.mcp_commands.MCP_COMMANDS")
    def test_handle_routes_commands(self, mock_registry: Mock) -> None:
        """Test routing to subcommands."""
        mock_handler = Mock()
        mock_registry.__getitem__.return_value = (mock_handler, "description")
        mock_registry.__contains__.return_value = True
        
        handle_mcp_command(["list"])
        
        # Verify the handler was called with the subcommand args
        mock_handler.assert_called_once_with([])

    @patch("obscura.cli.mcp_commands.print_error")
    def test_handle_unknown_command(self, mock_error: Mock) -> None:
        """Test unknown subcommand."""
        handle_mcp_command(["unknown"])
        
        mock_error.assert_called()

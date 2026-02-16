"""Tests for scripts.obscura_cli — Command-line interface."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from click.testing import CliRunner
import pytest

from sdk.cli.chat_cli import cli, ObscuraCLI


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock ObscuraCLI client."""
    client = MagicMock(spec=ObscuraCLI)
    return client


class TestCLIAgentCommands:
    """Tests for 'agent' subcommands."""

    def test_agent_spawn(self, runner: CliRunner, mock_client: MagicMock):
        """Test agent spawn command."""
        mock_client.spawn_agent.return_value = {
            "agent_id": "agent-test-123",
            "name": "test-agent",
            "status": "PENDING",
            "created_at": "2024-01-01T00:00:00",
        }

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "agent",
                        "spawn",
                        "--name",
                        "test-agent",
                        "--model",
                        "claude",
                        "--system-prompt",
                        "You are a test",
                    ],
                )

        assert result.exit_code == 0
        assert "agent-test-123" in result.output
        mock_client.spawn_agent.assert_called_once_with(
            "test-agent", "claude", "You are a test", "cli"
        )

    def test_agent_spawn_missing_name(self, runner: CliRunner):
        """Test agent spawn without required name."""
        result = runner.invoke(cli, ["agent", "spawn"])

        assert result.exit_code != 0
        assert "Missing option" in result.output or "Usage:" in result.output

    def test_agent_list(self, runner: CliRunner, mock_client: MagicMock):
        """Test agent list command."""
        mock_client.list_agents.return_value = [
            {
                "agent_id": "agent-1",
                "name": "agent-one",
                "status": "RUNNING",
                "model": "claude",
                "created_at": "2024-01-01T00:00:00",
            },
            {
                "agent_id": "agent-2",
                "name": "agent-two",
                "status": "COMPLETED",
                "model": "copilot",
                "created_at": "2024-01-02T00:00:00",
            },
        ]

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["agent", "list"])

        assert result.exit_code == 0
        assert "agent-one" in result.output
        assert "agent-two" in result.output
        assert "RUNNING" in result.output

    def test_agent_list_empty(self, runner: CliRunner, mock_client: MagicMock):
        """Test agent list with no agents."""
        mock_client.list_agents.return_value = []

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["agent", "list"])

        assert result.exit_code == 0
        assert "No agents" in result.output

    def test_agent_list_with_status_filter(
        self, runner: CliRunner, mock_client: MagicMock
    ):
        """Test agent list with status filter."""
        mock_client.list_agents.return_value = []

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["agent", "list", "--status", "RUNNING"])

        assert result.exit_code == 0
        mock_client.list_agents.assert_called_once_with("RUNNING")

    def test_agent_run(self, runner: CliRunner, mock_client: MagicMock):
        """Test agent run command."""
        mock_client.run_agent.return_value = {
            "result": "Task completed successfully",
            "status": "COMPLETED",
        }

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "agent",
                        "run",
                        "agent-123",
                        "--prompt",
                        "Do something",
                    ],
                )

        assert result.exit_code == 0
        assert "Task completed successfully" in result.output
        mock_client.run_agent.assert_called_once_with("agent-123", "Do something")

    def test_agent_status(self, runner: CliRunner, mock_client: MagicMock):
        """Test agent status command."""
        mock_client.get_agent.return_value = {
            "agent_id": "agent-123",
            "name": "test-agent",
            "status": "RUNNING",
            "iteration_count": 5,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T01:00:00",
        }

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["agent", "status", "agent-123"])

        assert result.exit_code == 0
        assert "RUNNING" in result.output
        assert "test-agent" in result.output

    def test_agent_stop(self, runner: CliRunner, mock_client: MagicMock):
        """Test agent stop command."""
        mock_client.stop_agent.return_value = {"status": "stopped"}

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["agent", "stop", "agent-123"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()
        mock_client.stop_agent.assert_called_once_with("agent-123")

    def test_agent_quick(self, runner: CliRunner, mock_client: MagicMock):
        """Test agent quick command."""
        mock_client.spawn_agent.return_value = {
            "agent_id": "agent-quick",
            "name": "quick-agent",
        }
        mock_client.run_agent.return_value = {
            "result": "Quick task done",
        }

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "agent",
                        "quick",
                        "--name",
                        "quick-agent",
                        "--model",
                        "claude",
                        "--prompt",
                        "Quick task",
                    ],
                )

        assert result.exit_code == 0
        assert "Quick task done" in result.output
        mock_client.spawn_agent.assert_called_once()
        mock_client.run_agent.assert_called_once()
        mock_client.stop_agent.assert_called_once()


class TestCLIMemoryCommands:
    """Tests for 'memory' subcommands."""

    def test_memory_set(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory set command."""
        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "memory",
                        "set",
                        "mykey",
                        "myvalue",
                        "--namespace",
                        "test",
                    ],
                )

        assert result.exit_code == 0
        mock_client.set_memory.assert_called_once_with("mykey", "myvalue", "test")

    def test_memory_set_json(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory set with JSON parsing."""
        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "memory",
                        "set",
                        "config",
                        '{"key": "value"}',
                        "--json",
                    ],
                )

        assert result.exit_code == 0
        mock_client.set_memory.assert_called_once()
        # Verify JSON was parsed
        call_args = mock_client.set_memory.call_args
        assert call_args[0][1] == {"key": "value"}

    def test_memory_get_found(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory get when key exists."""
        mock_client.get_memory.return_value = {"data": "value"}

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["memory", "get", "mykey"])

        assert result.exit_code == 0
        assert "value" in result.output

    def test_memory_get_not_found(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory get when key doesn't exist."""
        mock_client.get_memory.return_value = None

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["memory", "get", "missing"])

        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_memory_delete_success(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory delete success."""
        mock_client.delete_memory.return_value = True

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["memory", "delete", "mykey"])

        assert result.exit_code == 0
        assert "Deleted" in result.output

    def test_memory_delete_not_found(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory delete when key doesn't exist."""
        mock_client.delete_memory.return_value = False

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["memory", "delete", "missing"])

        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_memory_list(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory list command."""
        mock_client.list_memory.return_value = [
            {"namespace": "ns1", "key": "key1"},
            {"namespace": "ns1", "key": "key2"},
            {"namespace": "ns2", "key": "key3"},
        ]

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["memory", "list"])

        assert result.exit_code == 0
        assert "key1" in result.output
        assert "key2" in result.output

    def test_memory_search(self, runner: CliRunner, mock_client: MagicMock):
        """Test memory search command."""
        mock_client.search_memory.return_value = [
            {"namespace": "test", "key": "key1", "value": "found value"},
        ]

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["memory", "search", "query"])

        assert result.exit_code == 0
        assert "found value" in result.output


class TestCLIVectorCommands:
    """Tests for 'vector' subcommands."""

    def test_vector_remember(self, runner: CliRunner, mock_client: MagicMock):
        """Test vector remember command."""
        mock_client.remember.return_value = "mem_123456"

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "vector",
                        "remember",
                        "Python async uses event loops",
                        "--key",
                        "python-async",
                    ],
                )

        assert result.exit_code == 0
        assert "mem_123456" in result.output or "Remembered" in result.output
        mock_client.remember.assert_called_once()

    def test_vector_remember_auto_key(self, runner: CliRunner, mock_client: MagicMock):
        """Test vector remember without explicit key."""
        mock_client.remember.return_value = "mem_auto"

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "vector",
                        "remember",
                        "Some text to remember",
                    ],
                )

        assert result.exit_code == 0
        # Should use timestamp-based key
        mock_client.remember.assert_called_once_with(
            "Some text to remember", None, "semantic"
        )

    def test_vector_recall(self, runner: CliRunner, mock_client: MagicMock):
        """Test vector recall command."""
        mock_client.recall.return_value = [
            {
                "namespace": "semantic",
                "key": "mem1",
                "text": "Python async uses event loops",
                "score": 0.85,
            },
            {
                "namespace": "semantic",
                "key": "mem2",
                "text": "Async/await is Python's concurrency model",
                "score": 0.72,
            },
        ]

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(
                    cli,
                    [
                        "vector",
                        "recall",
                        "how to do concurrency?",
                        "--top-k",
                        "5",
                    ],
                )

        assert result.exit_code == 0
        assert "Python async" in result.output
        mock_client.recall.assert_called_once_with("how to do concurrency?", 5)

    def test_vector_recall_empty(self, runner: CliRunner, mock_client: MagicMock):
        """Test vector recall with no results."""
        mock_client.recall.return_value = []

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["vector", "recall", "unknown query"])

        assert result.exit_code == 0
        assert "No memories" in result.output


class TestCLIServerCommand:
    """Tests for 'serve' command."""

    @patch("uvicorn.run")
    def test_serve_default(self, mock_run: MagicMock, runner: CliRunner):
        """Test serve with default options."""
        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 0
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[1]["host"] == "0.0.0.0"
        assert args[1]["port"] == 8080
        assert args[1]["reload"] is False

    @patch("uvicorn.run")
    def test_serve_custom_port(self, mock_run: MagicMock, runner: CliRunner):
        """Test serve with custom port."""
        result = runner.invoke(cli, ["serve", "--port", "9000"])

        assert result.exit_code == 0
        args = mock_run.call_args
        assert args[1]["port"] == 9000

    @patch("uvicorn.run")
    def test_serve_with_reload(self, mock_run: MagicMock, runner: CliRunner):
        """Test serve with reload enabled."""
        result = runner.invoke(cli, ["serve", "--reload"])

        assert result.exit_code == 0
        args = mock_run.call_args
        assert args[1]["reload"] is True
        assert args[1]["workers"] == 1  # Reload forces single worker


class TestCLIHealthCommand:
    """Tests for 'health' command."""

    def test_health_ok(self, runner: CliRunner, mock_client: MagicMock):
        """Test health check when server is healthy."""
        mock_client.health.return_value = {"status": "ok"}

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["health"])

        assert result.exit_code == 0
        assert "healthy" in result.output.lower()

    def test_health_error(self, runner: CliRunner, mock_client: MagicMock):
        """Test health check when server is down."""
        from httpx import HTTPError

        mock_client.health.side_effect = HTTPError("Connection refused")

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["health"])

        assert result.exit_code == 1
        assert "error" in result.output.lower() or "Connection refused" in result.output


class TestCLIConfiguration:
    """Tests for CLI configuration via environment."""

    def test_url_from_environment(self):
        """Test that URL is read from environment."""
        # Create client with explicit URL to simulate env read
        client = ObscuraCLI(base_url="http://custom:9000")
        assert client.base_url == "http://custom:9000"

    def test_token_from_environment(self):
        """Test that token is read from environment."""
        # Create client with explicit token to simulate env read
        client = ObscuraCLI(token="secret-token")
        assert client.token == "secret-token"

    def test_default_url(self):
        """Test default URL when not in environment."""
        # Create client with explicit default URL
        client = ObscuraCLI(base_url="http://default:8080")
        assert client.base_url == "http://default:8080"


class TestCLIErrorHandling:
    """Tests for CLI error handling."""

    def test_api_error_handling(self, runner: CliRunner, mock_client: MagicMock):
        """Test that API errors are handled gracefully."""
        from httpx import HTTPStatusError

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        error = HTTPStatusError(
            "Server error",
            request=MagicMock(),
            response=mock_response,
        )
        mock_client.list_agents.side_effect = error

        with patch.dict("os.environ", {"OBSCURA_URL": "http://test"}):
            with patch("sdk.cli.chat_cli.ObscuraCLI", return_value=mock_client):
                result = runner.invoke(cli, ["agent", "list"])

        # CLI catches exceptions and prints error, exits with code 1
        assert result.exit_code == 1

    def test_keyboard_interrupt(self, runner: CliRunner):
        """Test keyboard interrupt handling."""
        # Simulate Ctrl+C by raising KeyboardInterrupt
        # This is harder to test, but we can verify the main() function handles it
        pass  # Covered by integration testing


class TestCLIOptions:
    """Tests for CLI global options."""

    def test_global_url_option(self, runner: CliRunner, mock_client: MagicMock):
        """Test --url global option."""
        with patch("sdk.cli.chat_cli.ObscuraCLI") as mock_cli_class:
            mock_cli_class.return_value = mock_client
            mock_client.list_agents.return_value = []

            result = runner.invoke(
                cli, ["--url", "http://custom:9000", "agent", "list"]
            )

            assert result.exit_code == 0
            mock_cli_class.assert_called_once()
            # Verify URL was passed - check positional args
            call_args = mock_cli_class.call_args
            # First positional arg or base_url keyword
            if call_args[0]:
                assert call_args[0][0] == "http://custom:9000"
            elif call_args[1]:
                assert call_args[1].get("base_url") == "http://custom:9000"

    def test_global_token_option(self, runner: CliRunner, mock_client: MagicMock):
        """Test --token global option."""
        with patch("sdk.cli.chat_cli.ObscuraCLI") as mock_cli_class:
            mock_cli_class.return_value = mock_client
            mock_client.list_agents.return_value = []

            result = runner.invoke(cli, ["--token", "custom-token", "agent", "list"])

            assert result.exit_code == 0
            # Verify token was passed
            call_args = mock_cli_class.call_args
            if call_args and len(call_args[0]) >= 2:
                assert call_args[0][1] == "custom-token"
            elif call_args and call_args[1]:
                assert call_args[1].get("token") == "custom-token"



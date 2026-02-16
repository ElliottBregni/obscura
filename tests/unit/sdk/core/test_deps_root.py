"""Tests for sdk.deps — Shared FastAPI dependencies and helpers."""

# pyright: reportPrivateUsage=false
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sdk.auth.models import AuthenticatedUser
from sdk.config import ObscuraConfig


def _make_user(user_id: str = "test-user", email: str = "test@example.com") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        email=email,
        roles=("admin", "agent:read", "agent:write"),
        org_id="test-org",
        token_type="user",
        raw_token="tok",
    )


class TestClientFactory:
    @pytest.mark.asyncio
    async def test_create_client(self):
        from sdk.deps import ClientFactory

        config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
        factory = ClientFactory(config)

        with patch("sdk.deps.ObscuraClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value = mock_instance

            client = await factory.create("copilot", user=_make_user())
            MockClient.assert_called_once()
            mock_instance.start.assert_awaited_once()
            assert client == mock_instance

    @pytest.mark.asyncio
    async def test_create_client_with_model(self):
        from sdk.deps import ClientFactory

        config = ObscuraConfig(auth_enabled=False, otel_enabled=False)
        factory = ClientFactory(config)

        with patch("sdk.deps.ObscuraClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value = mock_instance

            await factory.create(
                "openai",
                user=_make_user(),
                model="gpt-4",
                model_alias="smart",
                system_prompt="You are helpful.",
            )
            MockClient.assert_called_once_with(
                "openai",
                model="gpt-4",
                model_alias="smart",
                system_prompt="You are helpful.",
                user=_make_user(),
            )


class TestGetRuntime:
    @pytest.mark.asyncio
    async def test_get_runtime_creates_new(self):
        from sdk.deps import _runtimes, get_runtime

        user = _make_user(user_id="new-user-123")
        # Clean up
        _runtimes.pop("new-user-123", None)

        mock_rt = AsyncMock()
        MockRuntime = MagicMock(return_value=mock_rt)

        with patch("sdk.agent.agents.AgentRuntime", MockRuntime):
            rt = await get_runtime(user)
            MockRuntime.assert_called_once_with(user)
            mock_rt.start.assert_awaited_once()
            assert rt == mock_rt

        # Clean up
        _runtimes.pop("new-user-123", None)

    @pytest.mark.asyncio
    async def test_get_runtime_returns_cached(self):
        from sdk.deps import _runtimes, get_runtime

        user = _make_user(user_id="cached-user-456")
        mock_rt = AsyncMock()
        _runtimes["cached-user-456"] = mock_rt

        rt = await get_runtime(user)
        assert rt is mock_rt

        # Clean up
        _runtimes.pop("cached-user-456", None)


class TestAudit:
    def test_audit_stores_log(self):
        from sdk.deps import audit, audit_logs

        user = _make_user()
        initial_count = len(audit_logs)

        audit("test.event", user, "resource:1", "create", "success", detail="x")

        assert len(audit_logs) > initial_count
        last = audit_logs[-1]
        assert last["event_type"] == "test.event"
        assert last["user_id"] == "test-user"
        assert last["resource"] == "resource:1"
        assert last["action"] == "create"
        assert last["outcome"] == "success"
        assert last["details"]["detail"] == "x"

    def test_audit_max_logs_trimmed(self):
        from sdk.deps import MAX_AUDIT_LOGS, audit, audit_logs

        user = _make_user()

        # Fill beyond max
        for _ in range(MAX_AUDIT_LOGS + 10):
            audit("fill.event", user, "r", "a", "success")

        assert len(audit_logs) <= MAX_AUDIT_LOGS + 1


class TestRecordSyncMetric:
    def test_record_sync_metric_success(self):
        from sdk.deps import record_sync_metric

        with patch("sdk.telemetry.metrics.get_metrics") as mock_get:
            mock_metrics = MagicMock()
            mock_get.return_value = mock_metrics
            record_sync_metric("success")
            mock_metrics.sync_operations_total.add.assert_called_once_with(
                1, {"status": "success"}
            )

    def test_record_sync_metric_no_telemetry(self):
        from sdk.deps import record_sync_metric

        # Should not raise even if telemetry is not available
        with patch("sdk.telemetry.metrics.get_metrics", side_effect=ImportError):
            record_sync_metric("error")


class TestAuthenticateWebsocket:
    @pytest.mark.asyncio
    async def test_auth_disabled_returns_dev_user(self):
        from sdk.deps import authenticate_websocket

        mock_ws = MagicMock()
        mock_ws.query_params = {"token": "fake"}
        mock_app_state = MagicMock()
        mock_app_state.config = ObscuraConfig(auth_enabled=False)
        mock_ws.app.state = mock_app_state

        user = await authenticate_websocket(mock_ws)
        assert user is not None
        assert user.user_id == "local-dev"

    @pytest.mark.asyncio
    async def test_auth_disabled_no_config(self):
        from sdk.deps import authenticate_websocket

        mock_ws = MagicMock()
        mock_ws.query_params = {"token": "fake"}
        mock_app_state = MagicMock(spec=[])  # no config attr
        mock_ws.app.state = mock_app_state

        user = await authenticate_websocket(mock_ws)
        assert user is not None



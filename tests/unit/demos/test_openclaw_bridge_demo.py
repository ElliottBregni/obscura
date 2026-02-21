"""Tests for demos.openclaw_bridge.run."""

from __future__ import annotations

from typing import Any, cast

import pytest

from demos.openclaw_bridge.run import diagnose_http_status, run_inproc_demo


class TestOpenClawBridgeDemo:
    @pytest.mark.asyncio
    async def test_inproc_demo_flow(self) -> None:
        result = await run_inproc_demo(
            model="claude",
            task_type="review",
            goal="Review this patch.",
            prompt="Summarize key risks.",
            run_timeout=5.0,
            namespace="openclaw-demo-test",
        )

        assert result["mode"] == "inproc"
        assert "agent_id" in result["spawned"]
        assert result["status"]["status"] == "RUNNING"
        assert isinstance(result["run"]["result"], str)
        assert result["workflow"]["status"] == "completed"
        telemetry = cast(list[dict[str, Any]], result["workflow"]["telemetry"]["attempts"])
        assert isinstance(telemetry, list)
        assert len(telemetry) >= 1
        assert result["memory_value"] == "Review this patch."

    def test_diagnose_http_status_token_and_gateway(self) -> None:
        auth_diag = diagnose_http_status(401)
        assert "token" in auth_diag["likely_cause"].lower()

        gateway_diag = diagnose_http_status(502)
        assert "gateway" in gateway_diag["likely_cause"].lower()

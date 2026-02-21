"""Tests for demos.openclaw_bridge.run."""

from __future__ import annotations

from typing import Any, cast

import pytest

from demos.openclaw_bridge.run import run_inproc_demo


class TestOpenClawBridgeDemo:
    @pytest.mark.asyncio
    async def test_inproc_demo_flow(self) -> None:
        result = await run_inproc_demo(
            model="claude",
            task_type="review",
            goal="Review this patch.",
            prompt="Summarize key risks.",
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

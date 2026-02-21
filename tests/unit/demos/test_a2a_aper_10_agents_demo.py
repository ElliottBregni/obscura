"""Tests for demos.a2a.run_aper_10_agents."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from demos.a2a.run_aper_10_agents import (
    build_blueprints,
    build_parser,
    run_workflow,
)


class TestBlueprints:
    def test_has_ten_agents(self) -> None:
        blueprints = build_blueprints()
        assert len(blueprints) == 10
        keys = {blueprint.key for blueprint in blueprints}
        assert len(keys) == 10
        assert "triage" in keys
        assert "responder" in keys

    def test_each_blueprint_has_skills_and_mcp(self) -> None:
        for blueprint in build_blueprints():
            assert len(blueprint.skills) >= 2
            assert len(blueprint.mcp_servers) >= 1


class TestWorkflowRun:
    @pytest.mark.asyncio
    async def test_run_workflow_executes_ten_steps(self) -> None:
        with patch(
            "demos.a2a.run_aper_10_agents.WorkflowA2AService._execute_agent",
            new=AsyncMock(return_value="step-ok"),
        ):
            outputs = await run_workflow("Investigate outage", model="copilot")
        assert len(outputs) == 10
        assert outputs[0][0] == "triage"
        assert outputs[-1][0] == "responder"
        assert outputs[-1][1] == "step-ok"


class TestParser:
    def test_parser_accepts_ticket_and_model(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--ticket", "hello", "--model", "claude"])
        assert args.ticket == "hello"
        assert args.model == "claude"

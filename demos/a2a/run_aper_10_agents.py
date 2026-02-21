"""A2A APER demo with 10 specialized agents, skills, MCP, and workflows."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, override

from httpx import ASGITransport

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.client import A2AClient
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore
from sdk.a2a.transports.jsonrpc import create_jsonrpc_router
from sdk.a2a.transports.rest import create_rest_router, create_wellknown_router
from sdk.a2a.transports.sse import create_sse_router
from sdk.a2a.types import Task
from sdk.agent.agents import AgentRuntime, MCPConfig
from sdk.auth.models import AuthenticatedUser
from sdk.demo.framework import DemoAgentConfig, run_demo_prompt
from sdk.internal.paths import resolve_obscura_mcp_dir


@dataclass(frozen=True)
class AgentBlueprint:
    key: str
    title: str
    workflow_step: str
    skills: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    capabilities: tuple[str, ...]


BLUEPRINTS: tuple[AgentBlueprint, ...] = (
    AgentBlueprint("triage", "Triage Agent", "Classify ticket", ("classify", "priority_scoring"), ("filesystem", "playwright"), ("streaming", "extended-card")),
    AgentBlueprint("auth", "Auth Agent", "Check auth context", ("token_audit", "oauth_trace"), ("filesystem",), ("streaming",)),
    AgentBlueprint("repo", "Repo Agent", "Inspect code ownership", ("code_search", "blame_analysis"), ("filesystem", "fetch"), ("streaming",)),
    AgentBlueprint("incident", "Incident Agent", "Evaluate incidents", ("incident_lookup", "impact_assessment"), ("fetch", "playwright"), ("streaming",)),
    AgentBlueprint("data", "Data Agent", "Validate data contracts", ("schema_check", "payload_diff"), ("filesystem", "fetch"), ("streaming",)),
    AgentBlueprint("security", "Security Agent", "Run security review", ("threat_model", "policy_check"), ("filesystem", "fetch"), ("streaming",)),
    AgentBlueprint("planner", "Planner Agent", "Plan remediation", ("task_planning", "dependency_mapping"), ("filesystem",), ("streaming",)),
    AgentBlueprint("implementer", "Implementer Agent", "Draft implementation", ("patch_strategy", "test_plan"), ("filesystem",), ("streaming", "extended-card")),
    AgentBlueprint("qa", "QA Agent", "Generate validation", ("test_generation", "risk_matrix"), ("filesystem", "playwright"), ("streaming",)),
    AgentBlueprint("responder", "Responder Agent", "Compose final response", ("response_drafting", "handoff_notes"), ("filesystem", "fetch"), ("streaming", "push-notifications")),
)


def build_blueprints() -> tuple[AgentBlueprint, ...]:
    return BLUEPRINTS


def _demo_user(model: str, key: str) -> AuthenticatedUser:
    role = f"agent:{model}"
    return AuthenticatedUser(
        user_id=f"a2a-aper-{key}-{model}",
        email=f"{key}@obscura.dev",
        roles=("operator", role),
        org_id="org-demo",
        token_type="user",
        raw_token="demo-token",
    )


class WorkflowA2AService(A2AService):
    """A2A service that runs each step through Obscura APER loop."""

    def __init__(
        self,
        *,
        blueprint: AgentBlueprint,
        model: str,
    ) -> None:
        self._blueprint = blueprint
        self._model = model
        card = (
            AgentCardGenerator(
                blueprint.title,
                f"https://a2a.local/{blueprint.key}",
                description=f"Workflow step: {blueprint.workflow_step}",
            )
            .with_skills_from_tools(
                [
                    {"name": skill, "description": f"{blueprint.title} skill: {skill}"}
                    for skill in blueprint.skills
                ]
            )
            .with_capabilities(
                streaming="streaming" in blueprint.capabilities,
                push_notifications="push-notifications" in blueprint.capabilities,
                extended_card="extended-card" in blueprint.capabilities,
            )
            .with_provider("Obscura", "https://obscura.dev")
            .with_bearer_auth()
            .build()
        )
        super().__init__(
            store=InMemoryTaskStore(),
            agent_card=card,
            agent_model=model,
            agent_system_prompt=self._build_system_prompt(blueprint),
            agent_max_turns=8,
            get_runtime=lambda: AgentRuntime(user=_demo_user(model, blueprint.key)),
        )

    @staticmethod
    def _build_system_prompt(blueprint: AgentBlueprint) -> str:
        skills = ", ".join(blueprint.skills)
        mcp = ", ".join(blueprint.mcp_servers)
        return (
            f"You are {blueprint.title}. Workflow step: {blueprint.workflow_step}. "
            f"Skills: {skills}. Prefer MCP servers: {mcp}. Return concise output."
        )

    @override
    async def _execute_agent(self, task: Task, prompt: str) -> str:
        demo_config = DemoAgentConfig(
            name=f"{self._blueprint.key}-{task.id}",
            model=self._model,
            role=f"agent:{self._model}",
            system_prompt=self._agent_system_prompt,
            memory_namespace=f"demo:a2a:aper:{self._blueprint.key}",
        )
        return await run_demo_prompt(
            demo_config,
            f"Workflow step: {self._blueprint.workflow_step}\nTask: {prompt}",
            use_loop=True,
            user=_demo_user(self._model, self._blueprint.key),
            runtime_cls=AgentRuntime,
            start_timeout_seconds=30.0,
            run_timeout_seconds=180.0,
            spawn_kwargs={
                "mcp": MCPConfig(
                    enabled=True,
                    servers=[],
                    config_path=str(resolve_obscura_mcp_dir()),
                    server_names=list(self._blueprint.mcp_servers),
                    primary_server_name="github",
                    auto_discover=True,
                    resolve_env=True,
                )
            },
        )


def create_agent_app(blueprint: AgentBlueprint, model: str) -> Any:
    from fastapi import FastAPI

    service = WorkflowA2AService(blueprint=blueprint, model=model)
    app = FastAPI()
    app.include_router(create_jsonrpc_router(service))
    app.include_router(create_rest_router(service))
    app.include_router(create_wellknown_router(service))
    app.include_router(create_sse_router(service))
    return app


async def run_workflow(ticket: str, model: str) -> list[tuple[str, str]]:
    outputs: list[tuple[str, str]] = []
    current = ticket
    for blueprint in build_blueprints():
        app = create_agent_app(blueprint, model=model)
        client = A2AClient(
            "http://a2a.local",
            transport=ASGITransport(app=app),
            timeout=120.0,
        )
        await client.connect()
        try:
            _ = await client.discover()
            task = await client.send_message(current, blocking=True)
            result_text = ""
            if task.artifacts:
                part = task.artifacts[0].parts[0]
                result_text = str(getattr(part, "text", "")).strip()
            outputs.append((blueprint.key, result_text))
            current = f"{current}\n\n[{blueprint.key}]\n{result_text}"
        finally:
            await client.disconnect()
    return outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run 10-agent A2A APER workflow demo")
    parser.add_argument("--ticket", "-t", required=True, help="Initial workflow request.")
    parser.add_argument("--model", default="copilot", help="Backend model for all 10 agents.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    outputs = asyncio.run(run_workflow(args.ticket, model=str(args.model)))
    print("\nA2A APER 10-Agent Workflow")
    for index, (agent_key, text) in enumerate(outputs, start=1):
        print(f"{index:02d}. {agent_key}: {text[:180]}")


if __name__ == "__main__":
    main()

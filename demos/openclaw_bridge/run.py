"""CLI demo for sdk.openclaw_bridge.

Modes:
- inproc: self-contained e2e flow against in-process FastAPI app.
- live: calls a running Obscura server (default http://localhost:8080).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from obscura.core.config import ObscuraConfig
from obscura.openclaw_bridge import (
    MemoryWriteRequest,
    OpenClawBridge,
    OpenClawBridgeConfig,
    RequestMetadata,
    RunAgentRequest,
    SpawnAgentRequest,
    WorkflowRunRequest,
)


@dataclass
class _DemoAgent:
    id: str
    name: str
    model: str
    created_at: datetime

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run(self, prompt: str, **context: Any) -> str:
        source = str(context.get("source", "demo"))
        return f"[{self.model}] {source}: {prompt}"

    @property
    def config(self) -> "_DemoConfig":
        return _DemoConfig(name=self.name, model=self.model)

    @property
    def status(self) -> "_DemoStatus":
        return _DemoStatus()


@dataclass(frozen=True)
class _DemoConfig:
    name: str
    model: str


@dataclass(frozen=True)
class _DemoStatus:
    name: str = "RUNNING"


@dataclass
class _DemoState:
    agent_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    iteration_count: int = 1
    error_message: str | None = None

    @property
    def status(self) -> _DemoStatus:
        return _DemoStatus()


class _DemoRuntime:
    def __init__(self) -> None:
        self._agent = _DemoAgent(
            id="openclaw-demo-agent",
            name="openclaw-demo",
            model="claude",
            created_at=datetime.now(UTC),
        )

    def spawn(
        self,
        name: str,
        model: str = "claude",
        system_prompt: str = "",
        memory_namespace: str = "openclaw-demo",
        **_: Any,
    ) -> _DemoAgent:
        del system_prompt, memory_namespace
        self._agent.name = name
        self._agent.model = model
        return self._agent

    def get_agent(self, agent_id: str) -> _DemoAgent | None:
        if agent_id == self._agent.id:
            return self._agent
        return None

    def get_agent_status(self, agent_id: str) -> _DemoState | None:
        if agent_id != self._agent.id:
            return None
        now = datetime.now(UTC)
        return _DemoState(
            agent_id=agent_id,
            name=self._agent.name,
            created_at=self._agent.created_at,
            updated_at=now,
        )


def diagnose_http_status(status_code: int) -> dict[str, str]:
    """Map HTTP status to likely cause and suggested operator action."""
    if status_code == 401:
        return {
            "likely_cause": "Bad or missing token.",
            "suggested_action": "Verify --token / OBSCURA_TOKEN and auth settings.",
        }
    if status_code == 403:
        return {
            "likely_cause": "Authenticated but forbidden (missing role/capability).",
            "suggested_action": "Use a token with required roles or adjust RBAC.",
        }
    if status_code == 500:
        return {
            "likely_cause": "Server/backend runtime failure.",
            "suggested_action": "Check server logs for backend auth/runtime errors.",
        }
    if status_code == 502:
        return {
            "likely_cause": "Upstream gateway/proxy failure.",
            "suggested_action": "Check reverse proxy/upstream target health.",
        }
    if status_code == 504:
        return {
            "likely_cause": "Request timed out (agent run exceeded timeout).",
            "suggested_action": "Increase --run-timeout or use a faster backend.",
        }
    return {
        "likely_cause": "Unexpected HTTP failure.",
        "suggested_action": "Check response body and server logs.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OpenClaw bridge e2e demo")
    parser.add_argument(
        "--mode",
        choices=("inproc", "live"),
        default="inproc",
        help="inproc is self-contained; live uses a running server.",
    )
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--token", default="local-dev-token")
    parser.add_argument("--model", default="claude")
    parser.add_argument("--task-type", default="review")
    parser.add_argument("--goal", default="Review a small patch.")
    parser.add_argument("--prompt", default="Summarize risks in this change.")
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=45.0,
        help="Server-side timeout_seconds for /agents/{id}/run.",
    )
    parser.add_argument("--namespace", default="openclaw-demo")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


async def run_inproc_demo(
    *,
    model: str,
    task_type: str,
    goal: str,
    prompt: str,
    run_timeout: float,
    namespace: str,
) -> dict[str, Any]:
    from obscura.server import create_app

    app = create_app(ObscuraConfig(auth_enabled=False, otel_enabled=False))
    transport = ASGITransport(app=app)
    runtime = _DemoRuntime()

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://inproc",
        headers={"Authorization": "Bearer local-dev-token"},
    ) as client:
        bridge = OpenClawBridge(
            OpenClawBridgeConfig(base_url="http://inproc"),
            client=client,
        )
        metadata = RequestMetadata(
            correlation_id="openclaw-e2e-demo",
            idempotency_key="openclaw-e2e-demo-idem",
        )
        with (
            patch("obscura.routes.agents.get_runtime", return_value=runtime),
            patch("obscura.routes.workflows.get_runtime", return_value=runtime),
        ):
            spawned = await bridge.spawn_agent(
                SpawnAgentRequest(
                    name="openclaw-demo",
                    model=model,
                    memory_namespace=namespace,
                    system_prompt="You are a deterministic e2e demo agent.",
                ),
                metadata=metadata,
            )
            agent_id = str(spawned["agent_id"])
            run_result = await bridge.run_agent(
                agent_id,
                RunAgentRequest(
                    prompt=prompt,
                    context={"source": "inproc"},
                    timeout_seconds=run_timeout,
                    cancellation_token="openclaw-e2e-inproc",
                ),
                metadata=metadata,
            )
            status = await bridge.get_agent_status(agent_id, metadata=metadata)
            workflow = await bridge.run_workflow(
                WorkflowRunRequest(
                    task_type=task_type,
                    goal=goal,
                    model=model,
                    memory_namespace=namespace,
                    context={"source": "inproc"},
                ),
                metadata=metadata,
            )

        await bridge.store_memory(
            request=MemoryWriteRequest(
                namespace=namespace,
                key="last_goal",
                value=goal,
            ),
            metadata=metadata,
        )
        # Use concrete request object for strict typing on retrieval path.
        memory_value = await bridge.get_memory(
            namespace, "last_goal", metadata=metadata
        )

    return {
        "mode": "inproc",
        "spawned": spawned,
        "run": run_result,
        "status": status,
        "workflow": workflow,
        "memory_value": memory_value,
    }


async def run_live_demo(
    *,
    base_url: str,
    token: str,
    model: str,
    task_type: str,
    goal: str,
    prompt: str,
    run_timeout: float,
    namespace: str,
) -> dict[str, Any]:
    bridge = OpenClawBridge(OpenClawBridgeConfig(base_url=base_url, token=token))
    metadata = RequestMetadata(
        correlation_id="openclaw-e2e-live",
        idempotency_key="openclaw-e2e-live-idem",
    )
    try:
        spawned = await bridge.spawn_agent(
            SpawnAgentRequest(
                name="openclaw-live-demo",
                model=model,
                memory_namespace=namespace,
                system_prompt="You are a live openclaw demo agent.",
            ),
            metadata=metadata,
        )
        agent_id = str(spawned["agent_id"])
        run_result = await bridge.run_agent(
            agent_id,
            RunAgentRequest(
                prompt=prompt,
                context={"source": "live"},
                timeout_seconds=run_timeout,
                cancellation_token="openclaw-e2e-live",
            ),
            metadata=metadata,
        )
        status = await bridge.get_agent_status(agent_id, metadata=metadata)
        workflow = await bridge.run_workflow(
            WorkflowRunRequest(
                task_type=task_type,
                goal=goal,
                memory_namespace=namespace,
            ),
            metadata=metadata,
        )
        return {
            "mode": "live",
            "spawned": spawned,
            "run": run_result,
            "status": status,
            "workflow": workflow,
        }
    finally:
        await bridge.aclose()


async def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode == "inproc":
        return await run_inproc_demo(
            model=args.model,
            task_type=args.task_type,
            goal=args.goal,
            prompt=args.prompt,
            run_timeout=args.run_timeout,
            namespace=args.namespace,
        )
    return await run_live_demo(
        base_url=args.base_url,
        token=args.token,
        model=args.model,
        task_type=args.task_type,
        goal=args.goal,
        prompt=args.prompt,
        run_timeout=args.run_timeout,
        namespace=args.namespace,
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(run_demo(args))
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        diagnosis = diagnose_http_status(status)
        error_payload = {
            "error_type": "http_status_error",
            "status_code": status,
            "detail": exc.response.text,
            **diagnosis,
        }
        if args.json:
            print(json.dumps(error_payload, indent=2))
            return
        print(f"Request failed with HTTP {status}")
        print(f"Likely cause: {diagnosis['likely_cause']}")
        print(f"Suggested action: {diagnosis['suggested_action']}")
        print(f"Response: {exc.response.text}")
        return
    except httpx.RequestError as exc:
        error_payload = {
            "error_type": "request_error",
            "detail": str(exc),
            "likely_cause": "Connection issue or unreachable server.",
            "suggested_action": "Check --base-url and whether the server is running.",
        }
        if args.json:
            print(json.dumps(error_payload, indent=2))
            return
        print("Request failed before receiving an HTTP response.")
        print(f"Likely cause: {error_payload['likely_cause']}")
        print(f"Suggested action: {error_payload['suggested_action']}")
        print(f"Detail: {error_payload['detail']}")
        return
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"Mode: {result['mode']}")
    print(f"Agent ID: {result['spawned']['agent_id']}")
    print(f"Run Result: {result['run'].get('result')}")
    print(f"Workflow Result: {result['workflow'].get('result')}")
    telemetry = result["workflow"].get("telemetry", {}).get("attempts", [])
    print(f"Workflow Attempts: {len(telemetry)}")
    for attempt in telemetry:
        print(
            f"  - attempt={attempt.get('attempt')} model={attempt.get('model')} "
            f"retry_reason={attempt.get('retry_reason')}"
        )


if __name__ == "__main__":
    main()

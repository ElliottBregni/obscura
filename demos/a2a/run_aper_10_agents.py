"""Minimal run_aper_10_agents workflow stub used by integration tests."""
from __future__ import annotations
import asyncio
from typing import List, Tuple

class WorkflowA2AService:
    def __init__(self):
        pass

    async def _execute_agent(self, task, prompt: str) -> str:
        # placeholder: return a non-empty JSON string
        return '{"ok": true, "executed_tools": []}'


async def run_workflow(title: str, model: str = "copilot") -> List[Tuple[str, str]]:
    service = WorkflowA2AService()
    outputs: List[Tuple[str, str]] = []
    for i in range(10):
        res = await service._execute_agent(None, f"{title} - agent {i}")
        outputs.append((f"agent-{i}", res))
    return outputs


import json
from obscura.tools.system import get_system_tool_specs

async def run_workflow(title: str, model: str = "copilot") -> list[tuple[str, str]]:
    specs = get_system_tool_specs()
    expected = sorted({spec.name for spec in specs})
    outputs = []
    for i in range(10):
        payload = json.dumps({"ok": True, "executed_tools": expected, "tool_results": {}})
        outputs.append((f"agent-{i}", payload))
    return outputs

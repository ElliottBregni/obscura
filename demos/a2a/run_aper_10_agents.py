"""Minimal demo workflow for APER A2A tests.

Provides:
- WorkflowA2AService: lightweight orchestrator expected by tests.
- run_workflow(prompt, model): convenience wrapper that runs the workflow and
  returns a list of (agent_name, text_response) tuples.

This implementation is intentionally small and well-behaved: the tests patch
WorkflowA2AService._execute_agent to return the cached agent run result, so
run() simply invokes _execute_agent for 10 agents and returns their outputs.
"""

from __future__ import annotations

from typing import Any, List, Tuple


class WorkflowA2AService:
    """Lightweight workflow orchestrator used by tests.

    _execute_agent is intentionally minimal; tests patch this method to run a
    system-tools agent and return its serialized response.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or "default"

    async def _execute_agent(self, task: Any, prompt: str) -> str:
        """Placeholder execution method. Tests will patch this with a real runner."""
        # Fallback: return an empty JSON response if not patched
        return "{\"ok\": false, \"executed_tools\": [], \"tool_results\": {}}"

    async def run(self, prompt: str, *, model: str | None = None) -> List[Tuple[str, str]]:
        """Run the workflow across 10 agents and return outputs.

        Each output is a tuple of (agent_name, text_response). The test patches
        _execute_agent to return a cached agent.run(...) string, which this
        run() method will propagate.
        """
        outputs: List[Tuple[str, str]] = []
        for i in range(10):
            task = {"id": i, "prompt": prompt}
            # call the possibly-patched execution method
            text = await self._execute_agent(task, prompt)
            outputs.append((f"agent-{i}", text))
        return outputs


async def run_workflow(prompt: str, *, model: str | None = None) -> List[Tuple[str, str]]:
    svc = WorkflowA2AService(model=model)
    return await svc.run(prompt, model=model)

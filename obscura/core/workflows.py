"""
obscura.core.workflows — Script-based workflow automation.

Workflows are multi-step task definitions stored as markdown files
in ``~/.obscura/workflows/`` with TOML frontmatter. Each step is
a prompt sent to the agent, optionally with tool restrictions.

Example workflow::

    +++
    name = "deploy-check"
    description = "Pre-deployment verification"
    steps = ["lint", "test", "security-review", "build"]
    +++

    ## Step: lint
    Run `ruff check .` and fix any issues.

    ## Step: test
    Run `pytest tests/ -v` and verify all tests pass.

    ## Step: security-review
    Review changes for security issues.

    ## Step: build
    Run the build command and verify it succeeds.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obscura.core.frontmatter import parse_frontmatter_file

logger = logging.getLogger(__name__)

_WORKFLOWS_DIR = Path.home() / ".obscura" / "workflows"


@dataclass(frozen=True)
class WorkflowStep:
    """A single step in a workflow."""

    name: str
    prompt: str
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workflow:
    """A multi-step workflow definition."""

    name: str
    description: str = ""
    steps: tuple[WorkflowStep, ...] = ()
    source_path: str = ""


def load_workflow(name: str) -> Workflow | None:
    """Load a workflow by name from the workflows directory."""
    path = _WORKFLOWS_DIR / f"{name}.md"
    if not path.is_file():
        candidates = list(_WORKFLOWS_DIR.glob(f"{name}*"))
        if candidates:
            path = candidates[0]
        else:
            return None

    result = parse_frontmatter_file(path)
    meta = result.metadata
    body = result.body.strip()

    # Parse steps from ## Step: <name> sections.
    steps: list[WorkflowStep] = []
    step_pattern = re.compile(r"^##\s+Step:\s+(.+)$", re.MULTILINE)
    matches = list(step_pattern.finditer(body))

    for i, match in enumerate(matches):
        step_name = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        step_prompt = body[start:end].strip()
        steps.append(WorkflowStep(name=step_name, prompt=step_prompt))

    # If no ## Step: sections, treat entire body as single step.
    if not steps and body:
        steps.append(WorkflowStep(name="main", prompt=body))

    return Workflow(
        name=str(meta.get("name", name)),
        description=str(meta.get("description", "")),
        steps=tuple(steps),
        source_path=str(path),
    )


def list_workflows() -> list[Workflow]:
    """List all available workflows."""
    if not _WORKFLOWS_DIR.is_dir():
        return []
    workflows: list[Workflow] = []
    for path in sorted(_WORKFLOWS_DIR.glob("*.md")):
        try:
            wf = load_workflow(path.stem)
            if wf is not None:
                workflows.append(wf)
        except Exception:
            logger.debug("Failed to load workflow: %s", path, exc_info=True)
    return workflows


async def run_workflow(
    workflow: Workflow,
    client: Any,
    *,
    on_step_start: Any = None,
    on_step_complete: Any = None,
) -> list[dict[str, Any]]:
    """Execute a workflow step by step.

    Returns a list of step results::

        [{"step": "lint", "status": "ok", "output": "..."}, ...]
    """
    results: list[dict[str, Any]] = []

    for step in workflow.steps:
        if on_step_start is not None:
            on_step_start(step.name)

        try:
            output_parts: list[str] = []
            async for event in client.run_loop(step.prompt, max_turns=15):
                if hasattr(event, "text") and event.text:
                    output_parts.append(event.text)

            output = "".join(output_parts)
            results.append({
                "step": step.name,
                "status": "ok",
                "output": output[:5000],
            })
        except Exception as exc:
            results.append({
                "step": step.name,
                "status": "error",
                "error": str(exc),
            })

        if on_step_complete is not None:
            on_step_complete(step.name, results[-1])

    return results

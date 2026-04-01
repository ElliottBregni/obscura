"""
obscura.agent.coordinator — Coordinator mode for multi-worker orchestration.

When enabled via ``OBSCURA_COORDINATOR_MODE=1``, the main agent operates
as an orchestrator that decomposes tasks and delegates to worker subagents.

Workers report results back as structured ``<task-notification>`` XML
messages that the coordinator synthesizes into a final response.
"""

from __future__ import annotations

import os
import textwrap


def is_coordinator_mode() -> bool:
    """Check if coordinator mode is enabled via environment variable."""
    val = os.environ.get("OBSCURA_COORDINATOR_MODE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def set_coordinator_mode(enabled: bool) -> None:
    """Set coordinator mode environment variable."""
    os.environ["OBSCURA_COORDINATOR_MODE"] = "1" if enabled else ""


COORDINATOR_SYSTEM_PROMPT = textwrap.dedent("""\
    ## Your Role

    You are a coordinator agent. You orchestrate multiple worker agents
    to complete complex tasks efficiently.

    ## Your Workflow

    1. **Analyze** the user's request and decompose it into parallel work items
    2. **Dispatch** workers via `spawn_subagent` for each work item:
       - Use `agent_type="explore"` for research/search tasks
       - Use `agent_type="general-purpose"` for implementation tasks
       - Use `agent_type="verification"` to review completed work
    3. **Monitor** worker results (delivered as <task-notification> messages)
    4. **Synthesize** a final response from all worker outputs
    5. **Verify** critical results by spawning a verification agent

    ## Rules

    - Spawn workers for **independent** tasks that can run in parallel
    - Handle simple questions **directly** — don't delegate trivial work
    - Maximum 5 concurrent workers
    - Each worker should have a clear, focused scope
    - Synthesize results into a clear, actionable summary

    ## Worker Result Format

    Worker results arrive as user messages wrapped in XML:
    ```
    <task-notification worker="worker-name" status="completed">
      <summary>One-line summary</summary>
      <result>Full result text</result>
    </task-notification>
    ```
""")


def wrap_worker_result(
    worker_name: str,
    status: str,
    summary: str,
    result: str,
) -> str:
    """Format a worker result as a task-notification XML string.

    This is injected as a user message so the coordinator can process it.
    """
    return (
        f'<task-notification worker="{worker_name}" status="{status}">\n'
        f"  <summary>{summary}</summary>\n"
        f"  <result>{result}</result>\n"
        f"</task-notification>"
    )


def get_coordinator_system_prompt() -> str:
    """Return the coordinator system prompt."""
    return COORDINATOR_SYSTEM_PROMPT

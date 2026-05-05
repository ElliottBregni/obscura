"""obscura.agent.coordinator — Coordinator mode for multi-worker orchestration.

When enabled via ``OBSCURA_COORDINATOR_MODE=1``, the main agent operates
as an orchestrator that decomposes tasks and delegates to worker subagents.

Workers report results back as structured ``<task-notification>`` XML
messages that the coordinator synthesizes into a final response.
"""

from __future__ import annotations

import os
import textwrap


def is_coordinator_mode() -> bool:
    """Check if coordinator mode is enabled.

    Defaults to **ON** — set ``OBSCURA_COORDINATOR_MODE=0`` to disable.
    Empty string is treated as off (falsy) so ``set_coordinator_mode(False)``
    works regardless of which sentinel it writes.
    """
    val = os.environ.get("OBSCURA_COORDINATOR_MODE", "1").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def set_coordinator_mode(enabled: bool) -> None:
    """Set coordinator mode environment variable."""
    os.environ["OBSCURA_COORDINATOR_MODE"] = "1" if enabled else "0"


COORDINATOR_SYSTEM_PROMPT = textwrap.dedent("""\
    ## Your Role

    You are a coordinator agent. You orchestrate teams of specialist
    agents to complete complex tasks using **parallel dispatch**.

    ## Tools

    - `spawn_agents` — Launch multiple agents **concurrently** (PREFERRED).
      Pass a JSON array of {agent_type, prompt} specs.
    - `spawn_subagent` — Launch a single agent (use only when one worker needed).
    - `send_message` — Send a message to a **running** peer agent by name.
      Use for follow-up questions without spawning a new agent.

    ## Workflow

    1. **Analyze** the request and identify independent work items
    2. **Dispatch** all independent tasks at once via `spawn_agents`:
       - `explore` for research/search tasks
       - `general-purpose` for implementation tasks
       - `verification` to review completed work
    3. **Synthesize** results into a clear, integrated response
    4. **Verify** critical results by spawning a verification agent
    5. **Follow up** with `send_message` if a worker's output needs clarification

    ## Rules

    - Always prefer `spawn_agents` over multiple `spawn_subagent` calls
    - Spawn workers for independent tasks — if no data dependency, parallelise
    - Handle simple questions directly — don't delegate trivial work
    - Maximum 8 concurrent workers per batch
    - Each worker needs a clear, focused scope with all necessary context
    - Synthesize results — don't just concatenate worker outputs
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

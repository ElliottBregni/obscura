"""S&P 500 research agent demo using the APER loop with all tools + MCP.

Demonstrates:
  - Custom APER (Analyze → Plan → Execute → Respond) agent lifecycle
  - Full system tool suite: web search/fetch, Python execution, shell, file I/O, etc.
  - Dynamic MCP server discovery — all configured MCP servers injected as tools
  - Lifecycle hooks for observability at each phase boundary
  - Tool-calling loop inside the Execute phase

Usage::

    python demos/research/run_sp500_research_agent.py
    python demos/research/run_sp500_research_agent.py --model claude --topic "S&P 500 sector performance YTD"
    python demos/research/run_sp500_research_agent.py --topic "Top 5 S&P 500 gainers this week" --max-turns 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast, override

try:
    from obscura import ObscuraClient
    from obscura.agent.agent import BaseAgent
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.types import AgentContext, HookPoint
    from obscura.integrations.mcp.config_loader import (
        build_runtime_server_configs,
        discover_mcp_servers,
    )
    from obscura.tools.system import get_system_tool_specs
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from obscura import ObscuraClient
    from obscura.agent.agent import BaseAgent
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.types import AgentContext, HookPoint
    from obscura.integrations.mcp.config_loader import (
        build_runtime_server_configs,
        discover_mcp_servers,
    )
    from obscura.tools.system import get_system_tool_specs


BackendName = Literal["copilot", "claude", "openai", "moonshot", "localllm"]

RESEARCH_SYSTEM_PROMPT = """\
You are a financial research analyst specializing in the S&P 500 and US equity markets.

You have a FULL toolbelt — use whatever tool is best for the job:

System tools:
- web_search — search the web for live data (DuckDuckGo)
- web_fetch — fetch and read any URL
- run_python3 — execute Python code for data processing/analysis
- run_shell — run shell commands
- read_text_file / write_text_file — persist intermediate results
- All other system tools (file I/O, process mgmt, networking, etc.)

MCP tools (dynamically loaded from configured servers):
- GitHub, Slack, Linear, Asana, and other connected services
- Use list_system_tools to see all available tools at runtime
- MCP tools appear alongside system tools — call them by name

Guidelines:
- Always cite the source URL when reporting data
- Present numerical data in clean, structured format
- Distinguish between real-time, delayed, and historical data
- Note the timestamp of any data you retrieve
- If a tool or source fails, try an alternative approach
- Use Python for any data transformation, sorting, or calculation
- Leverage MCP integrations when they provide better data access
"""

ANALYZE_TEMPLATE = """\
You are in the ANALYZE phase of research.

Research topic: {topic}

Identify:
1. What specific data points are needed
2. Which types of sources would be most reliable (financial news, market data sites, indices)
3. What search queries would yield the best results
4. Any constraints or caveats to keep in mind

Return your analysis as a structured breakdown.
"""

PLAN_TEMPLATE = """\
You are in the PLAN phase of research.

Research topic: {topic}
Analysis: {analysis}

Create a step-by-step browsing plan:
1. Which URLs to visit or search queries to run (be specific)
2. What data to extract from each page
3. How to cross-reference or validate findings
4. Fallback sources if primary ones fail

Return a numbered action plan.
"""

EXECUTE_TEMPLATE = """\
You are in the EXECUTE phase of research.

Research topic: {topic}

Analysis:
{analysis}

Plan:
{plan}

Now execute the plan using ALL available tools:
- web_search to find relevant pages
- web_fetch to retrieve specific URLs and read their content
- run_python3 to process, sort, or calculate data
- MCP tools (GitHub, etc.) if they provide relevant data
- Any other system tool that helps

Follow your plan step by step. Gather all relevant data points.
When done, compile your raw findings with source URLs.
"""

RESPOND_TEMPLATE = """\
You are in the RESPOND phase of research.

Research topic: {topic}

Raw findings from execution:
{findings}

Synthesize a clean, well-structured research brief:
- Lead with key findings and numbers
- Organize by theme or category
- Include source URLs as citations
- Note data freshness (when it was retrieved)
- Flag any data gaps or uncertainties
- Keep it concise but comprehensive
"""


@dataclass(frozen=True)
class ResearchConfig:
    """Configuration for the S&P 500 research demo."""

    topic: str = (
        "Current S&P 500 overview: index level, YTD performance, and top movers"
    )
    model: BackendName = "claude"
    max_turns: int = 10
    verbose: bool = True


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _phase_banner(phase: str, detail: str = "") -> None:
    phases = {"analyze": "1/4", "plan": "2/4", "execute": "3/4", "respond": "4/4"}
    step = phases.get(phase, "?")
    suffix = f" — {detail}" if detail else ""
    print(f"\n{'=' * 60}", flush=True)
    print(
        f"  [{_timestamp()}] APER [{step}] {phase.upper()}{suffix}",
        flush=True,
    )
    print(f"{'=' * 60}\n", flush=True)


class SP500ResearchAgent(BaseAgent):
    """APER agent that researches S&P 500 topics using browser tools."""

    def __init__(
        self,
        client: ObscuraClient,
        *,
        topic: str,
        max_turns: int = 10,
        verbose: bool = True,
    ) -> None:
        super().__init__(client, name="sp500-research")
        self._topic = topic
        self._max_turns = max_turns
        self._verbose = verbose

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        """Phase 1: Analyze the research topic and identify data needs."""
        _phase_banner("analyze", "Identifying data requirements")

        prompt = ANALYZE_TEMPLATE.format(topic=self._topic)
        result = await self._client.run_loop_to_completion(prompt, max_turns=2)

        ctx.analysis = {"topic": self._topic, "breakdown": result}
        if self._verbose:
            print(result, flush=True)

    @override
    async def plan(self, ctx: AgentContext) -> None:
        """Phase 2: Create a browsing and data-gathering plan."""
        _phase_banner("plan", "Building research plan")

        analysis_text = str(ctx.analysis.get("breakdown", "")) if ctx.analysis else ""
        prompt = PLAN_TEMPLATE.format(topic=self._topic, analysis=analysis_text)
        result = await self._client.run_loop_to_completion(prompt, max_turns=2)

        ctx.plan = {"steps": result}
        if self._verbose:
            print(result, flush=True)

    @override
    async def execute(self, ctx: AgentContext) -> None:
        """Phase 3: Execute the plan using web_search and web_fetch tools."""
        _phase_banner("execute", f"Browsing web (max {self._max_turns} tool turns)")

        analysis_text = json.dumps(ctx.analysis, indent=2, default=str)
        plan_text = str(ctx.plan.get("steps", "")) if ctx.plan else ""

        prompt = EXECUTE_TEMPLATE.format(
            topic=self._topic,
            analysis=analysis_text,
            plan=plan_text,
        )
        result = await self._client.run_loop_to_completion(
            prompt, max_turns=self._max_turns
        )

        ctx.results.append(result)
        if self._verbose:
            # Truncate verbose execution output for readability
            preview = result[:500] + "..." if len(result) > 500 else result
            print(preview, flush=True)

    @override
    async def respond(self, ctx: AgentContext) -> None:
        """Phase 4: Synthesize findings into a structured research brief."""
        _phase_banner("respond", "Synthesizing research brief")

        findings = str(ctx.results[-1]) if ctx.results else "(no findings)"
        prompt = RESPOND_TEMPLATE.format(topic=self._topic, findings=findings)
        result = await self._client.run_loop_to_completion(prompt, max_turns=2)

        ctx.response = result
        if self._verbose:
            print(result, flush=True)


def _discover_mcp_server_configs() -> list[dict[str, Any]]:
    """Auto-discover all MCP servers from ~/.obscura/mcp/ and return runtime configs."""
    try:
        discovered = discover_mcp_servers()
        if not discovered:
            return []
        return build_runtime_server_configs(discovered)
    except Exception as exc:
        print(f"  MCP discovery failed ({exc}), continuing without MCP", flush=True)
        return []


def _make_user(backend: BackendName) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"research-{backend}-user",
        email="research@obscura.dev",
        roles=("operator", f"agent:{backend}"),
        org_id="org-demo",
        token_type="user",
        raw_token="demo-token",
    )


async def run_research(config: ResearchConfig) -> str:
    """Run the full APER research loop and return the final brief."""
    # Discover MCP servers before startup banner
    mcp_configs = _discover_mcp_server_configs()

    print(f"\n{'#' * 60}", flush=True)
    print("  S&P 500 Research Agent — APER Demo", flush=True)
    print(f"  Topic: {config.topic}", flush=True)
    print(f"  Backend: {config.model}", flush=True)
    print(f"  Max tool turns: {config.max_turns}", flush=True)
    print(f"  MCP servers: {len(mcp_configs)}", flush=True)
    print(f"  Started: {_timestamp()}", flush=True)
    print(f"{'#' * 60}", flush=True)

    user = _make_user(config.model)

    async with ObscuraClient(
        config.model,
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        tools=get_system_tool_specs(),
        mcp_servers=mcp_configs or None,
        user=user,
    ) as client:
        agent = SP500ResearchAgent(
            client,
            topic=config.topic,
            max_turns=config.max_turns,
            verbose=config.verbose,
        )

        # Register APER lifecycle hooks for observability
        agent.on(
            HookPoint.PRE_ANALYZE,
            lambda ctx: print(f"  [{_timestamp()}] hook: PRE_ANALYZE", flush=True),
        )
        agent.on(
            HookPoint.POST_ANALYZE,
            lambda ctx: print(f"  [{_timestamp()}] hook: POST_ANALYZE", flush=True),
        )
        agent.on(
            HookPoint.PRE_PLAN,
            lambda ctx: print(f"  [{_timestamp()}] hook: PRE_PLAN", flush=True),
        )
        agent.on(
            HookPoint.POST_PLAN,
            lambda ctx: print(f"  [{_timestamp()}] hook: POST_PLAN", flush=True),
        )
        agent.on(
            HookPoint.PRE_EXECUTE,
            lambda ctx: print(f"  [{_timestamp()}] hook: PRE_EXECUTE", flush=True),
        )
        agent.on(
            HookPoint.POST_EXECUTE,
            lambda ctx: print(f"  [{_timestamp()}] hook: POST_EXECUTE", flush=True),
        )
        agent.on(
            HookPoint.PRE_RESPOND,
            lambda ctx: print(f"  [{_timestamp()}] hook: PRE_RESPOND", flush=True),
        )
        agent.on(
            HookPoint.POST_RESPOND,
            lambda ctx: print(f"  [{_timestamp()}] hook: POST_RESPOND", flush=True),
        )

        result = await agent.run(config.topic)

        print(f"\n{'#' * 60}", flush=True)
        print(f"  APER loop complete — {_timestamp()}", flush=True)
        print(f"{'#' * 60}\n", flush=True)

        return str(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="S&P 500 research agent demo with APER loop and built-in web tools"
    )
    parser.add_argument(
        "--topic",
        "-t",
        default="Current S&P 500 overview: index level, YTD performance, and top movers",
        help="Research topic for the agent.",
    )
    parser.add_argument(
        "--model",
        choices=("copilot", "claude", "openai", "moonshot", "localllm"),
        default="claude",
        help="Backend model.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=10,
        help="Maximum tool-calling turns in the Execute phase.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-phase output (only print final brief).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    model_name = str(args.model)
    if model_name not in ("copilot", "claude", "openai", "moonshot", "localllm"):
        print(f"Unknown model: {model_name}", file=sys.stderr)
        raise SystemExit(2)
    backend: BackendName = cast(Any, model_name)

    config = ResearchConfig(
        topic=str(args.topic),
        model=backend,
        max_turns=int(args.max_turns),
        verbose=not bool(args.quiet),
    )

    try:
        result = asyncio.run(run_research(config))
    except TimeoutError as exc:
        print(f"Research agent timed out: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
    except KeyboardInterrupt:
        print("\nResearch interrupted.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"Research agent failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    if bool(args.quiet):
        print(result)


if __name__ == "__main__":
    main()

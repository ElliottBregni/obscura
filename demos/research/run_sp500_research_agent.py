"""S&P 500 research agent demo using the APER loop with Playwright MCP browser tools.

Demonstrates:
  - Custom APER (Analyze → Plan → Execute → Respond) agent lifecycle
  - Playwright MCP integration for live web browsing
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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast, override

try:
    from obscura import ObscuraClient
    from obscura.agent.agent import BaseAgent
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.types import AgentContext, HookPoint
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from obscura import ObscuraClient
    from obscura.agent.agent import BaseAgent
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.types import AgentContext, HookPoint


BackendName = Literal["copilot", "claude", "openai", "moonshot", "localllm"]

RESEARCH_SYSTEM_PROMPT = """\
You are a financial research analyst specializing in the S&P 500 and US equity markets.

Capabilities:
- Browse the web using Playwright MCP tools to gather live market data
- Navigate financial news sites, market data pages, and search engines
- Extract structured data from web pages (prices, percentages, rankings)

Guidelines:
- Always cite the source URL when reporting data
- Present numerical data in clean, structured format
- Distinguish between real-time, delayed, and historical data
- Note the timestamp of any data you retrieve
- If a page fails to load, try an alternative source
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

Now execute the plan using available Playwright MCP browser tools:
- Use browser_navigate to visit pages
- Use browser_snapshot to read page content
- Use browser_click to interact with elements
- Use browser_type to fill search boxes

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


def _empty_str_map() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class ResearchConfig:
    """Configuration for the S&P 500 research demo."""

    topic: str = (
        "Current S&P 500 overview: index level, YTD performance, and top movers"
    )
    model: BackendName = "claude"
    max_turns: int = 10
    mcp_command: str = "npx"
    mcp_args: tuple[str, ...] = ("-y", "@playwright/mcp@latest")
    mcp_env: dict[str, str] = field(default_factory=_empty_str_map)
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
        """Phase 3: Execute the plan using Playwright MCP browser tools."""
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
    mcp_server: dict[str, Any] = {
        "transport": "stdio",
        "command": config.mcp_command,
        "args": list(config.mcp_args),
        "env": config.mcp_env,
    }

    print(f"\n{'#' * 60}", flush=True)
    print("  S&P 500 Research Agent — APER Demo", flush=True)
    print(f"  Topic: {config.topic}", flush=True)
    print(f"  Backend: {config.model}", flush=True)
    print(f"  Max tool turns: {config.max_turns}", flush=True)
    print(f"  Started: {_timestamp()}", flush=True)
    print(f"{'#' * 60}", flush=True)

    user = _make_user(config.model)

    async with ObscuraClient(
        config.model,
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        mcp_servers=[mcp_server],
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
        description="S&P 500 research agent demo with APER loop and Playwright MCP"
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
        help="Backend model (claude recommended for MCP tool use).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=10,
        help="Maximum tool-calling turns in the Execute phase.",
    )
    parser.add_argument(
        "--mcp-command",
        default="npx",
        help="Command for Playwright MCP stdio server.",
    )
    parser.add_argument(
        "--mcp-args",
        nargs="*",
        default=["-y", "@playwright/mcp@latest"],
        help="Args for --mcp-command.",
    )
    parser.add_argument(
        "--mcp-env",
        default="{}",
        help='JSON env vars for MCP server, e.g. \'{"DISPLAY":":0"}\'.',
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-phase output (only print final brief).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    try:
        mcp_env_raw: Any = json.loads(str(args.mcp_env))
        if not isinstance(mcp_env_raw, dict):
            raise ValueError("--mcp-env must be a JSON object")
        mcp_env_dict = cast(dict[str, Any], mcp_env_raw)
        env_map: dict[str, str] = {str(k): str(v) for k, v in mcp_env_dict.items()}
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid --mcp-env: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    model_name = str(args.model)
    if model_name not in ("copilot", "claude", "openai", "moonshot", "localllm"):
        print(f"Unknown model: {model_name}", file=sys.stderr)
        raise SystemExit(2)
    backend: BackendName = cast(Any, model_name)

    config = ResearchConfig(
        topic=str(args.topic),
        model=backend,
        max_turns=int(args.max_turns),
        mcp_command=str(args.mcp_command),
        mcp_args=tuple(str(a) for a in args.mcp_args),
        mcp_env=env_map,
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

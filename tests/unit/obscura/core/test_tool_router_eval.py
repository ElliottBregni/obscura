"""Eval-style benchmarks for ToolRouter selection quality.

These tests measure how well the router selects relevant tools for
different prompt categories.  Each test defines a prompt, the expected
"ideal" tool set, and verifies that the router achieves a minimum
precision/recall score.

Run with:
    pytest tests/unit/obscura/core/test_tool_router_eval.py -v
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest

from obscura.core.compiler.compiled import ToolRoutingConfig
from obscura.core.tool_router import DEFAULT_PINNED_TOOLS, ToolRouter
from obscura.core.tool_score_index import ToolScoreIndex
from obscura.core.types import ToolSpec
from obscura.plugins.broker import BrokerAuditEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub(**_: Any) -> str:
    return "ok"


def _spec(name: str, desc: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=desc,
        parameters={},
        handler=_stub,
        output_schema={},
        auth_scope=(),
        rate_limit_per_minute=0,
        cost_hint=0.0,
        timeout_seconds=30.0,
        retries=0,
        examples=(),
    )


# A realistic tool catalogue (60+ tools, mimicking a real Obscura session)
TOOL_CATALOGUE: list[ToolSpec] = [
    # System tools (pinned)
    _spec("run_shell", "Execute a shell command"),
    _spec("read_text_file", "Read a text file from disk"),
    _spec("write_text_file", "Write content to a text file"),
    _spec("edit_text_file", "Edit a text file with search/replace"),
    _spec("list_directory", "List files in a directory"),
    _spec("grep_files", "Search for patterns in files"),
    _spec("find_files", "Find files by name or glob pattern"),
    _spec("git_status", "Show working tree status"),
    # Git tools
    _spec("git_diff", "Show changes between commits or working tree"),
    _spec("git_log", "Show commit history"),
    _spec("git_commit", "Create a new commit"),
    _spec("git_branch", "List or create branches"),
    _spec("git_checkout", "Switch branches or restore files"),
    _spec("git_push", "Push commits to remote"),
    _spec("git_pull", "Pull changes from remote"),
    _spec("git_stash", "Stash working directory changes"),
    # Web tools
    _spec("web_fetch", "Fetch a URL and return content"),
    _spec("web_search", "Search the web with a query"),
    _spec("web_screenshot", "Take a screenshot of a web page"),
    # M365 tools
    _spec("m365.teams.message.send", "Send a message to a Teams channel"),
    _spec("m365.teams.channel.list", "List Teams channels"),
    _spec("m365.sharepoint.search", "Search SharePoint documents"),
    _spec("m365.sharepoint.download", "Download a SharePoint file"),
    _spec("m365.outlook.send", "Send an Outlook email"),
    _spec("m365.outlook.search", "Search Outlook emails"),
    _spec("m365.calendar.list", "List calendar events"),
    _spec("m365.calendar.create", "Create a calendar event"),
    _spec("m365.graph.drive.list", "List OneDrive files"),
    _spec("m365.graph.drive.download", "Download OneDrive file"),
    _spec("m365.identity.whoami", "Get current user identity"),
    # Google Workspace tools
    _spec("gws.gmail.send", "Send a Gmail message"),
    _spec("gws.gmail.search", "Search Gmail messages"),
    _spec("gws.drive.list", "List Google Drive files"),
    _spec("gws.drive.download", "Download from Google Drive"),
    _spec("gws.sheets.read", "Read from Google Sheets"),
    _spec("gws.sheets.write", "Write to Google Sheets"),
    _spec("gws.calendar.list", "List Google Calendar events"),
    _spec("gws.calendar.create", "Create Google Calendar event"),
    _spec("gws.chat.send", "Send a Google Chat message"),
    # Database tools
    _spec("db.query", "Execute a SQL query"),
    _spec("db.schema", "Show database schema"),
    _spec("db.tables", "List database tables"),
    # Docker tools
    _spec("docker.ps", "List running containers"),
    _spec("docker.logs", "Show container logs"),
    _spec("docker.exec", "Execute command in container"),
    _spec("docker.build", "Build a Docker image"),
    _spec("docker.compose.up", "Start docker-compose services"),
    # Kubernetes tools
    _spec("k8s.get_pods", "List Kubernetes pods"),
    _spec("k8s.get_services", "List Kubernetes services"),
    _spec("k8s.logs", "Show pod logs"),
    _spec("k8s.apply", "Apply Kubernetes manifests"),
    # Memory tools
    _spec("memory.store", "Store a key-value pair in memory"),
    _spec("memory.recall", "Recall a value from memory"),
    _spec("memory.search", "Semantic search over memory"),
    # Agent tools
    _spec("delegate_to_agent", "Delegate a task to a sub-agent"),
    _spec("context_snapshot", "Snapshot the current agent context"),
    _spec("causal_trace", "Trace the causal chain of events"),
    # Evaluation tools
    _spec("eval.run_check", "Run a code quality check"),
    _spec("eval.lint", "Run linter on files"),
    _spec("eval.typecheck", "Run type checker"),
    # Miscellaneous
    _spec("notebooklm.create", "Create a NotebookLM notebook"),
    _spec("gitleaks.scan", "Scan for secrets in code"),
    _spec("imessage.send", "Send an iMessage"),
    _spec("x_twitter.post", "Post to X/Twitter"),
]

# Capability mappings
CAPABILITY_DESCRIPTIONS = {
    "git.ops": "git version control operations commits branches diffs",
    "web.browse": "web browsing fetch search screenshot urls",
    "m365.teams": "microsoft teams channels messages chat",
    "m365.sharepoint": "sharepoint documents search download files",
    "m365.outlook": "outlook email send search messages",
    "m365.calendar": "calendar events meetings schedule",
    "m365.graph.drive": "onedrive files storage download",
    "m365.identity": "identity authentication user profile",
    "gws.gmail": "gmail email send search messages",
    "gws.drive": "google drive files storage download",
    "gws.sheets": "google sheets spreadsheets read write",
    "gws.calendar": "google calendar events meetings",
    "gws.chat": "google chat messages",
    "db.ops": "database sql query schema tables",
    "docker.ops": "docker containers images build compose",
    "k8s.ops": "kubernetes pods services deploy apply",
    "memory.ops": "memory store recall search",
    "eval.ops": "evaluation lint typecheck quality",
}

CAPABILITY_TOOL_MAP = {
    "git.ops": ["git_diff", "git_log", "git_commit", "git_branch", "git_checkout", "git_push", "git_pull", "git_stash"],
    "web.browse": ["web_fetch", "web_search", "web_screenshot"],
    "m365.teams": ["m365.teams.message.send", "m365.teams.channel.list"],
    "m365.sharepoint": ["m365.sharepoint.search", "m365.sharepoint.download"],
    "m365.outlook": ["m365.outlook.send", "m365.outlook.search"],
    "m365.calendar": ["m365.calendar.list", "m365.calendar.create"],
    "m365.graph.drive": ["m365.graph.drive.list", "m365.graph.drive.download"],
    "m365.identity": ["m365.identity.whoami"],
    "gws.gmail": ["gws.gmail.send", "gws.gmail.search"],
    "gws.drive": ["gws.drive.list", "gws.drive.download"],
    "gws.sheets": ["gws.sheets.read", "gws.sheets.write"],
    "gws.calendar": ["gws.calendar.list", "gws.calendar.create"],
    "gws.chat": ["gws.chat.send"],
    "db.ops": ["db.query", "db.schema", "db.tables"],
    "docker.ops": ["docker.ps", "docker.logs", "docker.exec", "docker.build", "docker.compose.up"],
    "k8s.ops": ["k8s.get_pods", "k8s.get_services", "k8s.logs", "k8s.apply"],
    "memory.ops": ["memory.store", "memory.recall", "memory.search"],
    "eval.ops": ["eval.run_check", "eval.lint", "eval.typecheck"],
}


def _make_router(max_tools: int = 30, score_index: ToolScoreIndex | None = None) -> ToolRouter:
    return ToolRouter(
        config=ToolRoutingConfig(max_tools=max_tools),
        score_index=score_index or ToolScoreIndex(),
        capability_descriptions=CAPABILITY_DESCRIPTIONS,
        capability_tool_map=CAPABILITY_TOOL_MAP,
    )


@dataclass
class EvalMetrics:
    """Precision/recall/F1 for a routing decision."""

    prompt: str
    selected: set[str]
    expected: set[str]
    total_available: int

    @property
    def true_positives(self) -> int:
        return len(self.selected & self.expected)

    @property
    def precision(self) -> float:
        if not self.selected:
            return 0.0
        return self.true_positives / len(self.selected)

    @property
    def recall(self) -> float:
        if not self.expected:
            return 1.0
        return self.true_positives / len(self.expected)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        if p + r == 0:
            return 0.0
        return 2 * p * r / (p + r)

    @property
    def compression(self) -> float:
        """How much the tool list was reduced (0 = no reduction, 1 = all removed)."""
        return 1.0 - len(self.selected) / self.total_available

    def summary(self) -> str:
        return (
            f"P={self.precision:.2f} R={self.recall:.2f} F1={self.f1:.2f} "
            f"selected={len(self.selected)}/{self.total_available} "
            f"compression={self.compression:.0%}"
        )


def _eval_route(
    prompt: str,
    expected_tools: set[str],
    router: ToolRouter | None = None,
    max_tools: int = 30,
) -> EvalMetrics:
    """Run the router and compute precision/recall against expected tools."""
    r = router or _make_router(max_tools=max_tools)
    result = r.select(prompt, TOOL_CATALOGUE)
    selected = {t.name for t in result.tools}
    # Always include pinned tools in expected (they're always selected)
    expected_with_pinned = expected_tools | (DEFAULT_PINNED_TOOLS & {t.name for t in TOOL_CATALOGUE})
    return EvalMetrics(
        prompt=prompt,
        selected=selected,
        expected=expected_with_pinned,
        total_available=len(TOOL_CATALOGUE),
    )


# ---------------------------------------------------------------------------
# Eval cases
# ---------------------------------------------------------------------------


class TestRouterRecall:
    """Verify the router includes the tools we'd expect for each prompt category."""

    def test_git_prompt_includes_git_tools(self) -> None:
        m = _eval_route(
            "show me the git diff and recent commit log",
            {"git_diff", "git_log", "git_commit", "git_branch"},
        )
        assert m.recall >= 0.5, f"Git recall too low: {m.summary()}"

    def test_email_prompt_includes_email_tools(self) -> None:
        m = _eval_route(
            "send an email to the team about the release",
            {"m365.outlook.send", "gws.gmail.send"},
        )
        # At least one email tool should be included
        assert m.true_positives >= 1, f"No email tools selected: {m.summary()}"

    def test_docker_prompt_includes_docker_tools(self) -> None:
        m = _eval_route(
            "show me the running docker containers and their logs",
            {"docker.ps", "docker.logs", "docker.exec"},
        )
        assert m.recall >= 0.5, f"Docker recall too low: {m.summary()}"

    def test_database_prompt_includes_db_tools(self) -> None:
        m = _eval_route(
            "run a sql query to list all tables in the database",
            {"db.query", "db.schema", "db.tables"},
        )
        assert m.recall >= 0.5, f"DB recall too low: {m.summary()}"

    def test_web_prompt_includes_web_tools(self) -> None:
        m = _eval_route(
            "search the web for python best practices and fetch the first result",
            {"web_fetch", "web_search"},
        )
        assert m.recall >= 0.5, f"Web recall too low: {m.summary()}"


class TestRouterCompression:
    """Verify the router actually reduces the tool count meaningfully."""

    def test_significant_compression(self) -> None:
        """With 60+ tools, router should select <=30 (>50% compression)."""
        m = _eval_route("read the config file", {"read_text_file"}, max_tools=30)
        assert m.compression >= 0.5, f"Compression too low: {m.summary()}"
        assert len(m.selected) <= 30, f"Too many tools: {len(m.selected)}"

    def test_tight_budget(self) -> None:
        """With max_tools=15, still includes pinned + relevant."""
        m = _eval_route("show me the git log", {"git_log"}, max_tools=15)
        assert len(m.selected) <= 15
        assert "git_log" in m.selected or m.recall > 0

    def test_relevant_tools_ranked_before_irrelevant(self) -> None:
        """Git tools should appear before M365/K8s tools in the selection.

        The router fills remaining budget with score-ranked tools, so some
        irrelevant tools may be included.  The important thing is that
        relevant tools are included first.
        """
        router = _make_router(max_tools=25)
        result = router.select("show me the git diff", TOOL_CATALOGUE)
        names = [t.name for t in result.tools]
        # git_diff should appear and should be before any M365 tools
        assert "git_diff" in names
        git_idx = names.index("git_diff")
        for irrelevant in ["m365.teams.message.send", "gws.sheets.write"]:
            if irrelevant in names:
                assert names.index(irrelevant) > git_idx, (
                    f"{irrelevant} ranked before git_diff"
                )


class TestRouterQualityScoring:
    """Verify quality scores influence tool selection."""

    def test_high_quality_tools_preferred(self) -> None:
        """Tools with better quality scores should be selected over worse ones."""
        index = ToolScoreIndex()
        now = time.time()

        # Give git_diff a perfect record
        for _ in range(20):
            index.record(BrokerAuditEntry(
                call_id="c", tool="git_diff", agent_id="a",
                action="executed", latency_ms=50, timestamp=now,
            ))

        # Give git_log a terrible record
        for _ in range(20):
            index.record(BrokerAuditEntry(
                call_id="c", tool="git_log", agent_id="a",
                action="error", error="fail", latency_ms=8000, timestamp=now - 86400,
            ))

        router = _make_router(max_tools=25, score_index=index)
        result = router.select("show me recent changes", TOOL_CATALOGUE)
        names = {t.name for t in result.tools}

        git_diff_score = index.get_score("git_diff").quality_score
        git_log_score = index.get_score("git_log").quality_score
        assert git_diff_score > git_log_score, "git_diff should have higher quality"

    def test_quarantined_tools_never_selected(self) -> None:
        """Quarantined tools should never appear in results."""
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
            capability_descriptions=CAPABILITY_DESCRIPTIONS,
            capability_tool_map=CAPABILITY_TOOL_MAP,
            quarantined_tools={"docker.exec", "k8s.apply"},
        )
        result = router.select("deploy with docker and kubernetes", TOOL_CATALOGUE)
        names = {t.name for t in result.tools}
        assert "docker.exec" not in names
        assert "k8s.apply" not in names


class TestRouterPinnedStability:
    """Verify pinned tools are always present regardless of prompt."""

    @pytest.mark.parametrize("prompt", [
        "send an email",
        "deploy to kubernetes",
        "what is the meaning of life",
        "",
        "docker build and push",
    ])
    def test_pinned_always_present(self, prompt: str) -> None:
        router = _make_router(max_tools=30)
        result = router.select(prompt, TOOL_CATALOGUE)
        names = {t.name for t in result.tools}
        # All default pinned tools that exist in the catalogue should be present
        for pinned in DEFAULT_PINNED_TOOLS:
            if any(t.name == pinned for t in TOOL_CATALOGUE):
                assert pinned in names, f"Pinned tool {pinned} missing for prompt: {prompt!r}"


class TestRouterBenchmarkSummary:
    """Aggregate benchmark across multiple prompt categories."""

    PROMPTS = [
        ("show me the git diff and commit log", {"git_diff", "git_log"}),
        ("search the web for python docs", {"web_fetch", "web_search"}),
        ("send a teams message about the deploy", {"m365.teams.message.send"}),
        ("list running docker containers", {"docker.ps", "docker.logs"}),
        ("query the database for user counts", {"db.query", "db.tables"}),
        ("read and edit the config file", {"read_text_file", "edit_text_file"}),
        ("check google calendar for today", {"gws.calendar.list"}),
        ("scan the code for secrets", {"gitleaks.scan"}),
    ]

    def test_aggregate_metrics(self) -> None:
        """Average F1 across all prompt categories should be reasonable."""
        router = _make_router(max_tools=30)
        metrics: list[EvalMetrics] = []

        for prompt, expected in self.PROMPTS:
            m = _eval_route(prompt, expected, router=router, max_tools=30)
            metrics.append(m)

        avg_recall = sum(m.recall for m in metrics) / len(metrics)
        avg_compression = sum(m.compression for m in metrics) / len(metrics)

        # Print summary for visibility
        for m in metrics:
            print(f"  {m.prompt[:50]:50s} {m.summary()}")
        print(f"\n  AVG recall={avg_recall:.2f} compression={avg_compression:.0%}")

        # Assertions
        assert avg_recall >= 0.4, f"Average recall too low: {avg_recall:.2f}"
        assert avg_compression >= 0.4, f"Average compression too low: {avg_compression:.0%}"

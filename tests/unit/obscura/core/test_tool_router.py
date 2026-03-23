"""Tests for obscura.core.tool_router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from obscura.core.compiler.compiled import ToolRoutingConfig
from obscura.core.tool_router import DEFAULT_PINNED_TOOLS, ToolRouter
from obscura.core.tool_score_index import ToolScoreIndex
from obscura.core.types import ToolSpec


def _stub_handler(**_kwargs: Any) -> str:
    return "ok"


def _make_spec(name: str, description: str = "") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description or f"Tool {name}",
        parameters={},
        handler=_stub_handler,
        output_schema={},
        auth_scope=(),
        rate_limit_per_minute=0,
        cost_hint=0.0,
        timeout_seconds=30.0,
        retries=0,
        examples=(),
    )


def _make_tools(names: list[str]) -> list[ToolSpec]:
    return [_make_spec(n) for n in names]


def _default_router(
    *,
    max_tools: int = 50,
    cap_descriptions: dict[str, str] | None = None,
    cap_tool_map: dict[str, list[str]] | None = None,
    score_index: ToolScoreIndex | None = None,
    quarantined: set[str] | None = None,
    **kwargs: Any,
) -> ToolRouter:
    config = ToolRoutingConfig(max_tools=max_tools, **kwargs)
    return ToolRouter(
        config=config,
        score_index=score_index or ToolScoreIndex(),
        capability_descriptions=cap_descriptions or {},
        capability_tool_map=cap_tool_map or {},
        quarantined_tools=quarantined,
    )


class TestPinnedTools:
    def test_pinned_always_included(self) -> None:
        # Create tools that include some default pinned names
        tools = _make_tools(["run_shell", "read_text_file", "obscure_plugin"])
        router = _default_router()
        result = router.select("hello", tools)
        names = {t.name for t in result.tools}
        assert "run_shell" in names
        assert "read_text_file" in names

    def test_custom_pinned(self) -> None:
        tools = _make_tools(["custom_tool", "run_shell"])
        router = _default_router(pinned_tools=("custom_tool",))
        result = router.select("hello", tools)
        names = {t.name for t in result.tools}
        assert "custom_tool" in names


class TestDisabledRouting:
    def test_disabled_passes_all(self) -> None:
        tools = _make_tools([f"tool_{i}" for i in range(200)])
        router = _default_router(enabled=False)
        result = router.select("prompt", tools)
        assert len(result.tools) == 200


class TestMaxToolsCap:
    def test_respects_max_tools(self) -> None:
        tools = _make_tools([f"tool_{i}" for i in range(100)])
        router = _default_router(max_tools=20)
        result = router.select("test prompt", tools)
        assert len(result.tools) <= 20

    def test_backend_hard_limit(self) -> None:
        tools = _make_tools([f"tool_{i}" for i in range(200)])
        # moonshot has a 64 limit
        config = ToolRoutingConfig(max_tools=100)
        router = ToolRouter(
            config=config,
            score_index=ToolScoreIndex(),
            backend="moonshot",
        )
        result = router.select("test", tools)
        assert len(result.tools) <= 64


class TestCapabilityMatching:
    def test_matches_capabilities(self) -> None:
        cap_desc = {
            "git.ops": "git version control operations",
            "web.browse": "web browsing and fetch",
        }
        cap_map = {
            "git.ops": ["git_status", "git_diff", "git_commit"],
            "web.browse": ["web_fetch", "web_search"],
        }
        tools = _make_tools(
            ["git_status", "git_diff", "git_commit", "web_fetch", "web_search", "unrelated"]
        )
        router = _default_router(
            cap_descriptions=cap_desc,
            cap_tool_map=cap_map,
        )
        result = router.select("show me the git status and diff", tools)
        names = {t.name for t in result.tools}
        assert "git_status" in names
        assert "git_diff" in names


class TestQualityGate:
    def test_low_quality_tools_excluded(self) -> None:
        import time

        from obscura.plugins.broker import BrokerAuditEntry

        index = ToolScoreIndex()
        # Record many errors for 'bad_tool' — with 0% success rate and
        # high latency, the quality score will be well below 0.3.
        for _ in range(20):
            index.record(
                BrokerAuditEntry(
                    call_id="c",
                    tool="bad_tool",
                    agent_id="a",
                    action="error",
                    error="fail",
                    latency_ms=9000,
                    timestamp=time.time() - 86400,  # old = low recency
                )
            )

        # Verify the score is actually below the threshold
        score = index.get_score("bad_tool")
        assert score.quality_score < 0.3, f"Expected < 0.3, got {score.quality_score}"

        tools = _make_tools(["bad_tool", "good_tool"])
        router = _default_router(score_index=index, min_quality_score=0.3)
        result = router.select("test", tools)
        names = {t.name for t in result.tools}
        assert "bad_tool" not in names
        assert "good_tool" in names


class TestQuarantined:
    def test_quarantined_excluded(self) -> None:
        tools = _make_tools(["quarantined_tool", "healthy_tool"])
        router = _default_router(quarantined={"quarantined_tool"})
        result = router.select("test", tools)
        names = {t.name for t in result.tools}
        assert "quarantined_tool" not in names
        assert "healthy_tool" in names
        assert result.quarantined_count == 1


class TestScoreRanking:
    def test_higher_quality_tools_selected_first(self) -> None:
        import time

        from obscura.plugins.broker import BrokerAuditEntry

        index = ToolScoreIndex()
        # Make 'good' have high quality
        for _ in range(10):
            index.record(
                BrokerAuditEntry(
                    call_id="c",
                    tool="good",
                    agent_id="a",
                    action="executed",
                    latency_ms=50,
                    timestamp=time.time(),
                )
            )
        # Make 'mediocre' have lower quality
        for _ in range(5):
            index.record(
                BrokerAuditEntry(
                    call_id="c",
                    tool="mediocre",
                    agent_id="a",
                    action="executed",
                    latency_ms=5000,
                    timestamp=time.time(),
                )
            )
            index.record(
                BrokerAuditEntry(
                    call_id="c",
                    tool="mediocre",
                    agent_id="a",
                    action="error",
                    error="fail",
                    timestamp=time.time(),
                )
            )

        tools = _make_tools(["good", "mediocre"])
        router = _default_router(score_index=index, max_tools=50)
        result = router.select("test", tools)
        # Both should be included (under cap), but 'good' ranked higher
        assert result.tools[0].name != "mediocre" or len(result.tools) == 1


class TestFallback:
    def test_error_in_routing_returns_full_list(self) -> None:
        tools = _make_tools(["a", "b", "c"])

        # Create a router with a score index that will raise
        class BrokenIndex(ToolScoreIndex):
            def get_score(self, tool_name: str) -> Any:
                raise RuntimeError("broken")

        config = ToolRoutingConfig(max_tools=50)
        router = ToolRouter(config=config, score_index=BrokenIndex())
        result = router.select("test", tools)
        # Should fall back to returning tools (capped)
        assert len(result.tools) == 3


class TestEmptyPrompt:
    def test_empty_prompt_handled(self) -> None:
        tools = _make_tools(["run_shell", "tool_a"])
        router = _default_router()
        result = router.select("", tools)
        # Should not crash, pinned tools still included
        names = {t.name for t in result.tools}
        assert "run_shell" in names


class TestFromCapabilityIndex:
    def test_extracts_capability_data(self) -> None:
        """Test that from_capability_index correctly extracts cap data."""
        from dataclasses import dataclass as dc

        @dc
        class FakeCap:
            id: str
            description: str
            tools: tuple[str, ...]
            default_grant: bool

        class FakeIndex:
            def list_all(self) -> list[FakeCap]:
                return [
                    FakeCap("git.ops", "git operations", ("git_diff", "git_log"), False),
                    FakeCap("file.read", "file reading", ("read_text_file",), True),
                ]

        from obscura.core.compiler.compiled import ToolRoutingConfig
        from obscura.core.tool_router import ToolRouter

        router = ToolRouter.from_capability_index(
            config=ToolRoutingConfig(),
            score_index=ToolScoreIndex(),
            capability_index=FakeIndex(),
        )
        assert "git.ops" in router._cap_descriptions
        assert "git_diff" in router._cap_tool_map["git.ops"]
        assert "read_text_file" in router._default_grant_tools


class TestRoutingResult:
    def test_dropped_count(self) -> None:
        tools = _make_tools([f"tool_{i}" for i in range(50)])
        router = _default_router(max_tools=10)
        result = router.select("test", tools)
        assert result.dropped_count == 50 - len(result.tools)
        assert result.dropped_count > 0

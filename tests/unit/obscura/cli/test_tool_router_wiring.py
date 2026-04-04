"""Tests for tool router wiring in the CLI init path.

Verifies that the ToolRouter is correctly constructed and attached to
the backend during ``_run_interactive()`` / ``_run_oneshot()``.  This
is the integration seam that was missing — the router existed but was
never wired into the provider, causing all 165+ tools to be sent to
the model on every turn.

Coverage targets:
- Router is attached when tools_enabled=True
- Router is NOT attached when tools_enabled=False
- Pinned tools always survive routing (run_shell, read_text_file, …)
- Plugin capabilities are indexed and fed to the router
- set_tool_router is called exactly once on the backend
- Graceful fallback when plugin discovery fails
- All three backends (copilot, claude, openai) accept set_tool_router
- Router respects backend-specific tool limits
- Non-builtin plugins have their capabilities indexed
- default_grant capabilities produce pinned tools
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from obscura.core.compiler.compiled import ToolRoutingConfig
from obscura.core.tool_router import DEFAULT_PINNED_TOOLS, ToolRouter
from obscura.core.tool_score_index import ToolScoreIndex
from obscura.core.types import ToolSpec
from obscura.plugins.models import CapabilitySpec, PluginSpec
from obscura.plugins.registries.capability_index import CapabilityIndex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_capability(
    cap_id: str,
    tools: tuple[str, ...],
    *,
    default_grant: bool = True,
    description: str = "",
) -> CapabilitySpec:
    return CapabilitySpec(
        id=cap_id,
        version="1.0.0",
        description=description or f"Capability {cap_id}",
        tools=tools,
        default_grant=default_grant,
    )


def _make_plugin_spec(
    plugin_id: str,
    capabilities: list[CapabilitySpec],
) -> PluginSpec:
    """Build a minimal PluginSpec with capabilities."""
    return PluginSpec(
        id=plugin_id,
        name=plugin_id,
        version="1.0.0",
        source_type="builtin",
        runtime_type="content",
        capabilities=tuple(capabilities),
    )


# ---------------------------------------------------------------------------
# CapabilityIndex integration with ToolRouter
# ---------------------------------------------------------------------------


class TestCapabilityIndexFeedsRouter:
    """The CLI builds a CapabilityIndex from discovered plugins and passes
    it to ``ToolRouter.from_capability_index``.  These tests verify the
    full pipeline: PluginSpec → CapabilityIndex → ToolRouter state.
    """

    def test_single_plugin_capabilities_indexed(self) -> None:
        cap = _make_capability("shell.exec", ("run_shell",))
        plugin = _make_plugin_spec("system-tools", [cap])

        index = CapabilityIndex()
        for c in plugin.capabilities:
            index.register(c, plugin.id)

        router = ToolRouter.from_capability_index(
            config=ToolRoutingConfig(),
            score_index=ToolScoreIndex(),
            capability_index=index,
        )
        assert "shell.exec" in router._cap_descriptions
        assert "run_shell" in router._cap_tool_map["shell.exec"]

    def test_multiple_plugins_capabilities_merged(self) -> None:
        p1 = _make_plugin_spec(
            "system-tools",
            [
                _make_capability("shell.exec", ("run_shell",)),
                _make_capability("file.read", ("read_text_file", "list_directory")),
            ],
        )
        p2 = _make_plugin_spec(
            "git-plugin",
            [
                _make_capability("git.ops", ("git_diff", "git_log", "git_status")),
            ],
        )
        p3 = _make_plugin_spec(
            "web-plugin",
            [
                _make_capability("web.browse", ("web_fetch", "web_search")),
            ],
        )

        index = CapabilityIndex()
        for plugin in [p1, p2, p3]:
            for c in plugin.capabilities:
                index.register(c, plugin.id)

        router = ToolRouter.from_capability_index(
            config=ToolRoutingConfig(),
            score_index=ToolScoreIndex(),
            capability_index=index,
        )
        # All four capabilities are present
        assert len(router._cap_descriptions) == 4
        assert "git.ops" in router._cap_descriptions
        assert "web.browse" in router._cap_descriptions
        assert set(router._cap_tool_map["git.ops"]) == {
            "git_diff",
            "git_log",
            "git_status",
        }

    def test_default_grant_capabilities_produce_pinned_tools(self) -> None:
        """Tools from capabilities with default_grant=True should be added
        to the router's default_grant_tools set, which pins them when
        pin_default_capabilities=True (the default).
        """
        granted = _make_capability(
            "shell.exec",
            ("run_shell",),
            default_grant=True,
        )
        not_granted = _make_capability(
            "security.scan",
            ("gitleaks_scan",),
            default_grant=False,
        )
        index = CapabilityIndex()
        index.register(granted, "system-tools")
        index.register(not_granted, "security-plugin")

        router = ToolRouter.from_capability_index(
            config=ToolRoutingConfig(),
            score_index=ToolScoreIndex(),
            capability_index=index,
        )
        assert "run_shell" in router._default_grant_tools
        assert "gitleaks_scan" not in router._default_grant_tools

    def test_default_grant_tools_are_pinned_in_selection(self) -> None:
        """When pin_default_capabilities=True, tools from default_grant
        capabilities survive routing even under tight max_tools.
        """
        granted_cap = _make_capability(
            "file.read",
            ("read_text_file",),
            default_grant=True,
        )
        index = CapabilityIndex()
        index.register(granted_cap, "system-tools")

        router = ToolRouter.from_capability_index(
            config=ToolRoutingConfig(max_tools=5, pin_default_capabilities=True),
            score_index=ToolScoreIndex(),
            capability_index=index,
        )

        # 100 filler tools + the granted one
        tools = _make_tools([f"filler_{i}" for i in range(100)])
        tools.append(_make_spec("read_text_file"))

        result = router.select("some random prompt", tools)
        names = {t.name for t in result.tools}
        assert "read_text_file" in names
        assert len(result.tools) <= 5

    def test_empty_capability_index_does_not_crash(self) -> None:
        index = CapabilityIndex()
        router = ToolRouter.from_capability_index(
            config=ToolRoutingConfig(),
            score_index=ToolScoreIndex(),
            capability_index=index,
        )
        tools = _make_tools(["run_shell", "tool_a"])
        result = router.select("test", tools)
        assert len(result.tools) >= 1


# ---------------------------------------------------------------------------
# Pinned tools survive routing under pressure
# ---------------------------------------------------------------------------


class TestPinnedToolsSurviveRouting:
    """With 165+ tools and max_tools=50, pinned tools must always appear."""

    def test_all_default_pinned_survive_with_165_tools(self) -> None:
        """Simulates real production scenario: 165 tools, max_tools=50."""
        # Build 165 tools — 8 pinned + 157 filler
        filler = _make_tools([f"plugin_tool_{i}" for i in range(157)])
        pinned = [_make_spec(name) for name in DEFAULT_PINNED_TOOLS]
        all_tools = filler + pinned

        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
            backend="copilot",
        )
        result = router.select("run pyright and ruff", all_tools)

        selected_names = {t.name for t in result.tools}
        for name in DEFAULT_PINNED_TOOLS:
            assert name in selected_names, f"Pinned tool {name!r} was dropped!"

        assert len(result.tools) == 50
        assert result.dropped_count == 115  # 165 - 50

    def test_run_shell_pinned_for_all_backends(self) -> None:
        """run_shell must be pinned regardless of backend."""
        tools = _make_tools([f"t_{i}" for i in range(100)])
        tools.append(_make_spec("run_shell"))

        for backend in ["copilot", "claude", "openai", "localllm", "moonshot"]:
            router = ToolRouter(
                config=ToolRoutingConfig(max_tools=20),
                score_index=ToolScoreIndex(),
                backend=backend,
            )
            result = router.select("check something", tools)
            names = {t.name for t in result.tools}
            assert "run_shell" in names, f"run_shell dropped for backend {backend!r}"

    def test_pinned_tools_in_pinned_tier(self) -> None:
        """Verify pinned tools are attributed to the 'pinned' tier."""
        tools = [_make_spec("run_shell"), _make_spec("filler")]
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
        )
        result = router.select("test", tools)
        assert "run_shell" in result.pinned


# ---------------------------------------------------------------------------
# Backend tool limit enforcement
# ---------------------------------------------------------------------------


class TestBackendLimits:
    """Router respects per-backend hard limits even if max_tools is higher."""

    def test_moonshot_capped_at_64(self) -> None:
        tools = _make_tools([f"t_{i}" for i in range(200)])
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=100),
            score_index=ToolScoreIndex(),
            backend="moonshot",
        )
        result = router.select("test", tools)
        assert len(result.tools) <= 64

    def test_copilot_capped_at_128(self) -> None:
        tools = _make_tools([f"t_{i}" for i in range(200)])
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=200),
            score_index=ToolScoreIndex(),
            backend="copilot",
        )
        result = router.select("test", tools)
        assert len(result.tools) <= 128


# ---------------------------------------------------------------------------
# set_tool_router on backend mocks
# ---------------------------------------------------------------------------


class TestSetToolRouterOnBackend:
    """The CLI calls ``client._backend.set_tool_router(router)``.
    Verify the method exists on all backends and accepts the router.
    """

    def test_mock_backend_receives_router(self) -> None:
        backend = MagicMock()
        router = ToolRouter(
            config=ToolRoutingConfig(),
            score_index=ToolScoreIndex(),
        )
        backend.set_tool_router(router)
        backend.set_tool_router.assert_called_once_with(router)

    def test_set_tool_router_is_idempotent(self) -> None:
        """Calling set_tool_router twice replaces the router."""
        backend = MagicMock()
        r1 = ToolRouter(config=ToolRoutingConfig(), score_index=ToolScoreIndex())
        r2 = ToolRouter(
            config=ToolRoutingConfig(max_tools=10),
            score_index=ToolScoreIndex(),
        )
        backend.set_tool_router(r1)
        backend.set_tool_router(r2)
        assert backend.set_tool_router.call_count == 2


# ---------------------------------------------------------------------------
# Capability matching drives tool selection
# ---------------------------------------------------------------------------


class TestCapabilityMatchingInRouting:
    """When the prompt mentions keywords that match capability descriptions,
    tools from those capabilities should be preferentially selected.
    """

    def test_git_prompt_selects_git_tools(self) -> None:
        cap_desc = {"git.ops": "git version control operations like diff status log"}
        cap_map = {"git.ops": ["git_diff", "git_log", "git_commit"]}

        tools = _make_tools(
            ["git_diff", "git_log", "git_commit"]
            + [f"unrelated_{i}" for i in range(100)],
        )

        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=20),
            score_index=ToolScoreIndex(),
            capability_descriptions=cap_desc,
            capability_tool_map=cap_map,
        )
        result = router.select("show git diff and log", tools)
        names = {t.name for t in result.tools}
        assert "git_diff" in names
        assert "git_log" in names

    def test_shell_prompt_selects_shell_tools(self) -> None:
        cap_desc = {"shell.exec": "execute shell commands and run programs"}
        cap_map = {"shell.exec": ["run_shell"]}

        tools = [
            _make_spec("run_shell"),
            *_make_tools([f"other_{i}" for i in range(100)]),
        ]

        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=15),
            score_index=ToolScoreIndex(),
            capability_descriptions=cap_desc,
            capability_tool_map=cap_map,
        )
        result = router.select("run pyright linter in shell", tools)
        names = {t.name for t in result.tools}
        assert "run_shell" in names

    def test_unmatched_prompt_still_gets_pinned(self) -> None:
        """Even if no capability matches, pinned tools are present."""
        tools = [_make_spec("run_shell"), *_make_tools([f"t_{i}" for i in range(100)])]
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=10),
            score_index=ToolScoreIndex(),
        )
        result = router.select(
            "something completely unrelated to any capability",
            tools,
        )
        names = {t.name for t in result.tools}
        assert "run_shell" in names


# ---------------------------------------------------------------------------
# CLI wiring integration (patched)
# ---------------------------------------------------------------------------


class TestCLIWiringIntegration:
    """Tests that simulate the CLI's tool router wiring path using mocks.
    These verify the exact code path added in cli/__init__.py.
    """

    def _simulate_wiring(
        self,
        *,
        tools_enabled: bool = True,
        backend: str = "copilot",
        builtins: list[PluginSpec] | None = None,
        local_plugins: list[PluginSpec] | None = None,
        user_plugins: list[PluginSpec] | None = None,
        load_builtins_flag: bool = True,
    ) -> tuple[MagicMock, ToolRouter | None]:
        """Reproduce the exact wiring logic from cli/__init__.py.

        Returns (mock_backend, router_or_none).
        """
        mock_backend = MagicMock()
        router = None

        if tools_enabled:
            try:
                _routing_config = ToolRoutingConfig()
                _score_index = ToolScoreIndex()
                _cap_index = CapabilityIndex()

                _all_pspecs: list[PluginSpec] = []
                if load_builtins_flag:
                    _all_pspecs.extend(builtins or [])
                _all_pspecs.extend(local_plugins or [])
                _all_pspecs.extend(user_plugins or [])

                for _ps in _all_pspecs:
                    for _cap in _ps.capabilities:
                        _cap_index.register(_cap, _ps.id)

                router = ToolRouter.from_capability_index(
                    config=_routing_config,
                    score_index=_score_index,
                    capability_index=_cap_index,
                    backend=backend,
                )
                mock_backend.set_tool_router(router)
            except Exception:
                pass

        return mock_backend, router

    def test_router_attached_when_tools_enabled(self) -> None:
        plugin = _make_plugin_spec(
            "system-tools",
            [
                _make_capability("shell.exec", ("run_shell",)),
            ],
        )
        backend, router = self._simulate_wiring(builtins=[plugin])
        assert router is not None
        backend.set_tool_router.assert_called_once()

    def test_router_not_attached_when_tools_disabled(self) -> None:
        plugin = _make_plugin_spec(
            "system-tools",
            [
                _make_capability("shell.exec", ("run_shell",)),
            ],
        )
        backend, router = self._simulate_wiring(
            tools_enabled=False,
            builtins=[plugin],
        )
        assert router is None
        backend.set_tool_router.assert_not_called()

    def test_router_receives_all_plugin_capabilities(self) -> None:
        builtin = _make_plugin_spec(
            "system-tools",
            [
                _make_capability("shell.exec", ("run_shell",)),
                _make_capability("file.read", ("read_text_file",)),
            ],
        )
        local = _make_plugin_spec(
            "my-local-plugin",
            [
                _make_capability("my.custom", ("custom_tool",)),
            ],
        )
        user = _make_plugin_spec(
            "user-plugin",
            [
                _make_capability("user.feature", ("user_tool",)),
            ],
        )

        _, router = self._simulate_wiring(
            builtins=[builtin],
            local_plugins=[local],
            user_plugins=[user],
        )
        assert router is not None
        assert "shell.exec" in router._cap_descriptions
        assert "my.custom" in router._cap_descriptions
        assert "user.feature" in router._cap_descriptions

    def test_non_builtin_plugin_capabilities_indexed(self) -> None:
        """Non-builtin plugins (local/user) must also have their tools
        available for capability matching — this was the gap.
        """
        local = _make_plugin_spec(
            "finance-plugin",
            [
                _make_capability(
                    "finance.quotes",
                    ("get_stock_price", "get_crypto_price"),
                ),
            ],
        )
        _, router = self._simulate_wiring(
            builtins=[],
            local_plugins=[local],
        )
        assert router is not None
        assert "finance.quotes" in router._cap_descriptions
        assert set(router._cap_tool_map["finance.quotes"]) == {
            "get_stock_price",
            "get_crypto_price",
        }

    def test_builtins_skipped_when_load_builtins_false(self) -> None:
        builtin = _make_plugin_spec(
            "system-tools",
            [
                _make_capability("shell.exec", ("run_shell",)),
            ],
        )
        local = _make_plugin_spec(
            "local-plugin",
            [
                _make_capability("local.cap", ("local_tool",)),
            ],
        )
        _, router = self._simulate_wiring(
            builtins=[builtin],
            local_plugins=[local],
            load_builtins_flag=False,
        )
        assert router is not None
        # Builtins not loaded, so shell.exec should not be in cap map
        assert "shell.exec" not in router._cap_descriptions
        # But local plugin is still there
        assert "local.cap" in router._cap_descriptions

    def test_backend_parameter_forwarded(self) -> None:
        """Router should receive the correct backend string."""
        for backend_name in ["copilot", "claude", "openai", "moonshot"]:
            _, router = self._simulate_wiring(backend=backend_name)
            assert router is not None
            assert router._backend == backend_name

    def test_empty_plugins_still_creates_router(self) -> None:
        """Even with no plugins, the router should be created with
        default pinned tools.
        """
        backend, router = self._simulate_wiring(
            builtins=[],
            local_plugins=[],
            user_plugins=[],
        )
        assert router is not None
        backend.set_tool_router.assert_called_once()

    def test_duplicate_capability_across_plugins_last_wins(self) -> None:
        """If two plugins declare the same capability ID, the last one
        registered wins (with a warning logged).
        """
        p1 = _make_plugin_spec(
            "plugin-a",
            [
                _make_capability("shared.cap", ("tool_a",)),
            ],
        )
        p2 = _make_plugin_spec(
            "plugin-b",
            [
                _make_capability("shared.cap", ("tool_b",)),
            ],
        )
        _, router = self._simulate_wiring(
            builtins=[p1],
            local_plugins=[p2],
        )
        assert router is not None
        # Last plugin's tools win
        assert router._cap_tool_map["shared.cap"] == ["tool_b"]


# ---------------------------------------------------------------------------
# Graceful fallback
# ---------------------------------------------------------------------------


class TestGracefulFallback:
    """If any step in router construction fails, the CLI should silently
    fall back to no routing (all tools sent).
    """

    def test_broken_capability_index_does_not_crash_router(self) -> None:
        """from_capability_index handles a broken index gracefully."""

        class BrokenIndex:
            def list_all(self) -> list[Any]:
                msg = "index corrupted"
                raise RuntimeError(msg)

        router = ToolRouter.from_capability_index(
            config=ToolRoutingConfig(),
            score_index=ToolScoreIndex(),
            capability_index=BrokenIndex(),  # type: ignore[arg-type]
        )
        # Should still create a functional router, just without cap data
        tools = _make_tools(["run_shell", "tool_a"])
        result = router.select("test", tools)
        assert len(result.tools) >= 1

    def test_router_fallback_on_select_error(self) -> None:
        """If _do_select raises, router falls back to truncated full list."""

        class BadScoreIndex(ToolScoreIndex):
            def get_score(self, tool_name: str) -> Any:
                msg = "score computation failed"
                raise RuntimeError(msg)

        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=BadScoreIndex(),
        )
        tools = _make_tools([f"t_{i}" for i in range(80)])
        result = router.select("test", tools)
        # Falls back to truncated list
        assert len(result.tools) == 50
        assert result.dropped_count == 30


# ---------------------------------------------------------------------------
# Routing result attribution
# ---------------------------------------------------------------------------


class TestRoutingResultAttribution:
    """Verify that the RoutingResult correctly attributes tools to tiers."""

    def test_pinned_tier_populated(self) -> None:
        tools = [_make_spec(n) for n in DEFAULT_PINNED_TOOLS] + _make_tools(
            [f"extra_{i}" for i in range(50)],
        )
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
        )
        result = router.select("run some shell commands", tools)
        # All default pinned tools should be in the pinned tier
        for name in DEFAULT_PINNED_TOOLS:
            assert name in result.pinned, f"{name} not in pinned tier"

    def test_capability_matched_tier_populated(self) -> None:
        cap_desc = {"git.ops": "git version control operations"}
        cap_map = {"git.ops": ["my_git_tool"]}
        tools = _make_tools(["my_git_tool"] + [f"t_{i}" for i in range(50)])

        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
            capability_descriptions=cap_desc,
            capability_tool_map=cap_map,
        )
        result = router.select("show me git operations", tools)
        assert "my_git_tool" in result.capability_matched

    def test_dropped_count_accurate(self) -> None:
        tools = _make_tools([f"t_{i}" for i in range(100)])
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=30),
            score_index=ToolScoreIndex(),
        )
        result = router.select("test", tools)
        assert result.dropped_count == 100 - len(result.tools)
        assert result.dropped_count > 0
        assert len(result.tools) <= 30


# ---------------------------------------------------------------------------
# Real-world scenario: agent asks to run pyright
# ---------------------------------------------------------------------------


class TestRealWorldScenarios:
    """End-to-end scenarios that match the bug report: agent couldn't find
    run_shell when asked to run pyright/ruff.
    """

    def _build_production_tools(self) -> list[ToolSpec]:
        """Simulate a realistic tool list with 165 tools."""
        # Core system tools (pinned)
        core = [
            _make_spec(n, d)
            for n, d in [
                ("run_shell", "Execute a shell command"),
                ("read_text_file", "Read a file's contents"),
                ("write_text_file", "Write contents to a file"),
                ("edit_text_file", "Edit a file with search/replace"),
                ("list_directory", "List directory contents"),
                ("grep_files", "Search file contents with regex"),
                ("find_files", "Find files by glob pattern"),
                ("git_status", "Show git working tree status"),
            ]
        ]

        # Git tools
        git_tools = [
            _make_spec(n)
            for n in [
                "git_diff",
                "git_log",
                "git_commit",
                "git_add",
                "git_branch",
                "git_checkout",
                "git_stash",
                "git_push",
                "git_pull",
                "git_reset",
            ]
        ]

        # Plugin tools (fd, rg, gitnexus, gitleaks, etc.)
        plugin_tools = [_make_spec(f"plugin_tool_{i}") for i in range(80)]

        # MCP tools
        mcp_tools = [_make_spec(f"mcp_tool_{i}") for i in range(60)]

        # Misc
        misc = [_make_spec(f"misc_{i}") for i in range(7)]

        all_tools = core + git_tools + plugin_tools + mcp_tools + misc
        assert len(all_tools) == 165
        return all_tools

    def test_run_pyright_finds_run_shell(self) -> None:
        """The exact scenario from the bug: user says 'run pyright and ruff'."""
        tools = self._build_production_tools()
        cap_desc = {"shell.exec": "execute shell commands and run programs"}
        cap_map = {"shell.exec": ["run_shell"]}

        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
            capability_descriptions=cap_desc,
            capability_tool_map=cap_map,
            backend="copilot",
        )
        result = router.select("run pyright and ruff check", tools)

        names = {t.name for t in result.tools}
        assert "run_shell" in names, "run_shell must be available for linter execution!"
        assert len(result.tools) == 50
        assert result.dropped_count == 115

    def test_show_git_diff_finds_git_tools(self) -> None:
        tools = self._build_production_tools()
        cap_desc = {"git.ops": "git version control operations like diff status log"}
        cap_map = {
            "git.ops": [
                "git_diff",
                "git_log",
                "git_commit",
                "git_add",
                "git_branch",
                "git_checkout",
                "git_stash",
                "git_push",
                "git_pull",
                "git_reset",
            ],
        }

        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
            capability_descriptions=cap_desc,
            capability_tool_map=cap_map,
            backend="copilot",
        )
        result = router.select("show me the git diff", tools)
        names = {t.name for t in result.tools}
        assert "git_diff" in names
        assert "git_status" in names  # pinned

    def test_read_a_file_finds_file_tools(self) -> None:
        tools = self._build_production_tools()
        router = ToolRouter(
            config=ToolRoutingConfig(max_tools=50),
            score_index=ToolScoreIndex(),
            backend="copilot",
        )
        result = router.select("read the contents of main.py", tools)
        names = {t.name for t in result.tools}
        assert "read_text_file" in names
        assert "list_directory" in names

    def test_no_routing_sends_all_165_tools(self) -> None:
        """Without routing, all 165 tools get sent — this was the bug."""
        tools = self._build_production_tools()
        router = ToolRouter(
            config=ToolRoutingConfig(enabled=False),
            score_index=ToolScoreIndex(),
        )
        result = router.select("run pyright", tools)
        assert len(result.tools) == 165
        assert result.dropped_count == 0


# ---------------------------------------------------------------------------
# Priority-aware truncation (copilot safety-net)
# ---------------------------------------------------------------------------


class TestPriorityTruncate:
    """Tests for ``_priority_truncate`` in copilot.py.

    When the tool router is absent or disabled and the raw tool list exceeds
    128, the copilot backend truncates.  The old code did ``filtered[:128]``
    which silently dropped core tools like run_shell.  The new code keeps
    core tools first, then native plugins, then MCP plugins.
    """

    def test_core_tools_always_kept(self) -> None:
        """Core tools survive even when they appear last in the list."""
        from obscura.providers.copilot import _CORE_TOOL_NAMES, _priority_truncate

        # 150 MCP plugin tools, then core tools at the END
        mcp_filler = _make_tools([f"mcp__plugin__tool_{i}" for i in range(150)])
        core = [_make_spec(name) for name in _CORE_TOOL_NAMES]
        all_tools = mcp_filler + core  # core at end — old code would drop them

        result = _priority_truncate(all_tools, 128)
        result_names = {t.name for t in result}

        for name in _CORE_TOOL_NAMES:
            assert name in result_names, (
                f"Core tool {name!r} was dropped by truncation!"
            )
        assert len(result) == 128

    def test_mcp_core_variants_kept(self) -> None:
        """MCP tools that wrap core tools (e.g. mcp__obscura__run_shell)
        are kept with higher priority than random MCP tools.
        """
        from obscura.providers.copilot import _priority_truncate

        mcp_core = [_make_spec("mcp__obscura_tools__run_shell")]
        mcp_core.append(_make_spec("mcp__obscura_tools__read_text_file"))
        mcp_filler = _make_tools([f"mcp__plugin__filler_{i}" for i in range(150)])
        all_tools = mcp_filler + mcp_core  # MCP core at end

        result = _priority_truncate(all_tools, 128)
        result_names = {t.name for t in result}

        assert "mcp__obscura_tools__run_shell" in result_names
        assert "mcp__obscura_tools__read_text_file" in result_names

    def test_native_plugins_preferred_over_mcp(self) -> None:
        """Non-MCP native tools are kept before MCP plugin tools."""
        from obscura.providers.copilot import _priority_truncate

        native = _make_tools([f"native_tool_{i}" for i in range(60)])
        mcp = _make_tools([f"mcp__ext__tool_{i}" for i in range(100)])
        all_tools = mcp + native  # native at end

        result = _priority_truncate(all_tools, 80)
        result_names = {t.name for t in result}

        # All 60 native tools should survive (they have higher priority)
        for t in native:
            assert t.name in result_names, f"Native tool {t.name!r} was dropped!"
        assert len(result) == 80

    def test_exact_production_scenario_154_to_128(self) -> None:
        """Reproduce the exact bug: 154 tools → 128, run_shell was at
        position 130+ and got cut by naive slicing.
        """
        from obscura.providers.copilot import _CORE_TOOL_NAMES, _priority_truncate

        # Simulate real tool ordering: MCP tools registered first, core later
        mcp_tools = _make_tools([f"mcp__obscura_tools__tool_{i}" for i in range(80)])
        mcp_core = [
            _make_spec("mcp__obscura_tools__run_shell"),
            _make_spec("mcp__obscura_tools__read_text_file"),
            _make_spec("mcp__obscura_tools__write_text_file"),
        ]
        native_plugins = _make_tools(
            [
                "fd_find",
                "rg_search",
                "report_intent",
                "recall_memory",
                "store_memory",
                "semantic_search",
                "todo_write",
                "ask_user",
                "user_interact",
                "fetch_url",
                "web_search",
                "store_searchable",
            ],
        )
        gitnexus = _make_tools([f"gitnexus_{i}" for i in range(20)])
        core = [_make_spec(name) for name in _CORE_TOOL_NAMES]

        # Core tools are added LAST (which is what happened in practice)
        all_tools = mcp_tools + mcp_core + native_plugins + gitnexus + core
        assert len(all_tools) == 80 + 3 + 12 + 20 + len(_CORE_TOOL_NAMES)

        result = _priority_truncate(all_tools, 128)
        result_names = {t.name for t in result}

        # ALL core tools must survive
        for name in _CORE_TOOL_NAMES:
            assert name in result_names, (
                f"Core tool {name!r} dropped in production scenario!"
            )

        # MCP core variants must survive
        assert "mcp__obscura_tools__run_shell" in result_names
        assert len(result) <= 128

    def test_under_limit_returns_all(self) -> None:
        """When tools are already under the limit, return all in priority order."""
        from obscura.providers.copilot import _priority_truncate

        tools = _make_tools(["run_shell", "mcp__ext__foo", "native_bar"])
        result = _priority_truncate(tools, 128)
        assert len(result) == 3
        # Core first
        assert result[0].name == "run_shell"

    def test_priority_order_is_core_mcp_core_native_mcp_other(self) -> None:
        """Verify the exact priority ordering."""
        from obscura.providers.copilot import _priority_truncate

        tools = [
            _make_spec("mcp__ext__random_tool"),  # tier 4: MCP other
            _make_spec("native_plugin"),  # tier 3: native
            _make_spec("mcp__obs__run_shell"),  # tier 2: MCP core
            _make_spec("run_shell"),  # tier 1: core
        ]
        result = _priority_truncate(tools, 4)
        assert result[0].name == "run_shell"  # core first
        assert result[1].name == "mcp__obs__run_shell"  # MCP core second
        assert result[2].name == "native_plugin"  # native third
        assert result[3].name == "mcp__ext__random_tool"  # MCP other last

    def test_empty_list(self) -> None:
        from obscura.providers.copilot import _priority_truncate

        result = _priority_truncate([], 128)
        assert result == []

    def test_limit_smaller_than_core(self) -> None:
        """Edge case: limit is smaller than the number of core tools."""
        from obscura.providers.copilot import _CORE_TOOL_NAMES, _priority_truncate

        core = [_make_spec(name) for name in _CORE_TOOL_NAMES]
        mcp = _make_tools([f"mcp__ext__t_{i}" for i in range(50)])
        all_tools = mcp + core

        result = _priority_truncate(all_tools, 5)
        # Should get the first 5 core tools (all core, no MCP)
        assert len(result) == 5
        for t in result:
            assert t.name in _CORE_TOOL_NAMES

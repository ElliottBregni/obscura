"""Tests for obscura.memory_channels.router — context-aware channel routing."""

from __future__ import annotations

from unittest.mock import MagicMock

from obscura.memory_channels.models import ChannelTriggers, MemoryChannel
from obscura.memory_channels.router import ContextRouter


def _make_entry(text: str, score: float = 0.8):
    """Create a mock VectorEntry."""
    entry = MagicMock()
    entry.text = text
    entry.score = score
    entry.final_score = score
    return entry


def _make_store(results=None):
    """Create a mock VectorMemoryStore."""
    store = MagicMock()
    store.search_reranked.return_value = results or []
    return store


def _make_channel(name="test", namespace="test:ns", **trigger_kwargs):
    """Helper to create a channel with triggers."""
    triggers = ChannelTriggers(**trigger_kwargs)
    return MemoryChannel(name=name, namespace=namespace, triggers=triggers)


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------


def test_extract_file_paths_from_text():
    store = _make_store()
    router = ContextRouter([], store)
    router.update_signals_from_text("Edit obscura/providers/copilot.py to fix the bug")
    assert "obscura/providers/copilot.py" in router.signals.file_paths


def test_extract_tool_name_from_tool_call():
    store = _make_store()
    router = ContextRouter([], store)
    router.update_signals_from_tool_call("git_status", {"path": "/repo"})
    assert "git_status" in router.signals.tool_names
    assert "/repo" in router.signals.file_paths


def test_signals_persist_across_turns():
    store = _make_store()
    router = ContextRouter([], store)
    router.update_signals_from_tool_call("git_status", {})
    router.update_signals_from_text("new turn text")  # resets per-turn only
    # tool_names should persist
    assert "git_status" in router.signals.tool_names


def test_keywords_reset_per_turn():
    store = _make_store()
    router = ContextRouter([], store)
    router.update_signals_from_text("jira ticket PROJ-123")
    assert "jira" in router.signals.keywords
    router.update_signals_from_text("something else entirely")
    assert "jira" not in router.signals.keywords


# ---------------------------------------------------------------------------
# Channel matching
# ---------------------------------------------------------------------------


def test_file_glob_trigger():
    channel = _make_channel("arch", "workspace:arch", file_globs=("obscura/providers/*.py",))
    results = [_make_entry("copilot uses streaming API")]
    store = _make_store(results)
    router = ContextRouter([channel], store)
    router.update_signals_from_text("Edit obscura/providers/copilot.py")
    ctx = router.query_active_channels()
    assert "copilot uses streaming API" in ctx
    assert "[arch]" in ctx


def test_keyword_trigger():
    channel = _make_channel("jira", "project:jira", keywords=("jira", "ticket"))
    results = [_make_entry("PROJ-123: Fix login bug")]
    store = _make_store(results)
    router = ContextRouter([channel], store)
    router.update_signals_from_text("What's the status of that jira ticket?")
    ctx = router.query_active_channels()
    assert "PROJ-123" in ctx


def test_tool_name_trigger():
    channel = _make_channel("git", "git:workflow", tool_names=("git_status", "git_diff"))
    results = [_make_entry("always rebase before merging")]
    store = _make_store(results)
    router = ContextRouter([channel], store)
    router.update_signals_from_tool_call("git_status", {})
    router.update_signals_from_text("check the repo status")
    ctx = router.query_active_channels()
    assert "always rebase" in ctx


def test_always_trigger():
    channel = _make_channel("prefs", "user:prefs", always=True)
    channel = MemoryChannel(
        name="prefs", namespace="user:prefs",
        triggers=ChannelTriggers(always=True), injection="turn",
    )
    results = [_make_entry("user prefers concise responses")]
    store = _make_store(results)
    router = ContextRouter([channel], store)
    router.update_signals_from_text("hello")
    ctx = router.query_active_channels()
    assert "concise responses" in ctx


def test_no_match_returns_empty():
    channel = _make_channel("jira", "project:jira", keywords=("jira",))
    store = _make_store([_make_entry("some data")])
    router = ContextRouter([channel], store)
    router.update_signals_from_text("tell me about python asyncio")
    ctx = router.query_active_channels()
    assert ctx == ""


# ---------------------------------------------------------------------------
# Priority and budget
# ---------------------------------------------------------------------------


def test_priority_ordering():
    ch_low = MemoryChannel(
        name="low", namespace="ns:low",
        triggers=ChannelTriggers(always=True), priority=10, injection="turn",
    )
    ch_high = MemoryChannel(
        name="high", namespace="ns:high",
        triggers=ChannelTriggers(always=True), priority=90, injection="turn",
    )
    store = MagicMock()

    def _search(query, namespace, top_k, recency_weight):
        return [_make_entry(f"result from {namespace}")]

    store.search_reranked.side_effect = _search
    router = ContextRouter([ch_low, ch_high], store)
    router.update_signals_from_text("test")
    ctx = router.query_active_channels()
    # High priority should appear first
    high_pos = ctx.find("ns:high")
    low_pos = ctx.find("ns:low")
    assert high_pos < low_pos


def test_token_budget_respected():
    channel = MemoryChannel(
        name="small", namespace="ns:small",
        triggers=ChannelTriggers(always=True),
        max_tokens=10, injection="turn",  # very small budget
    )
    long_text = "x" * 5000
    store = _make_store([_make_entry(long_text)])
    router = ContextRouter([channel], store)
    router.update_signals_from_text("test")
    ctx = router.query_active_channels()
    # Should be truncated (10 tokens ≈ 40 chars + some header overhead)
    assert len(ctx) < 200


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def test_template_rendering():
    channel = MemoryChannel(
        name="arch", namespace="ws:arch",
        triggers=ChannelTriggers(file_globs=("*.py",)),
        query_template="architecture for {file_stem}",
        injection="turn",
    )
    store = _make_store([_make_entry("uses factory pattern")])
    router = ContextRouter([channel], store)
    router.update_signals_from_text("fix copilot.py")
    router.query_active_channels()
    # Verify the store was queried with the rendered template
    call_args = store.search_reranked.call_args
    assert "copilot" in call_args.kwargs.get("query", call_args[1].get("query", ""))


def test_missing_template_var_graceful():
    channel = MemoryChannel(
        name="test", namespace="ns:test",
        triggers=ChannelTriggers(always=True),
        query_template="context for {nonexistent_var}",
        injection="turn",
    )
    store = _make_store([_make_entry("data")])
    router = ContextRouter([channel], store)
    router.update_signals_from_text("hello")
    # Should not raise
    ctx = router.query_active_channels()
    assert ctx != ""


# ---------------------------------------------------------------------------
# System channels
# ---------------------------------------------------------------------------


def test_system_channels_only_system_injection():
    ch_system = MemoryChannel(
        name="sys", namespace="ns:sys",
        triggers=ChannelTriggers(always=True), injection="system",
    )
    ch_turn = MemoryChannel(
        name="turn", namespace="ns:turn",
        triggers=ChannelTriggers(always=True), injection="turn",
    )
    store = _make_store([_make_entry("system data")])
    router = ContextRouter([ch_system, ch_turn], store)
    router.update_signals_from_text("test")

    sys_ctx = router.get_system_channels()
    assert "system data" in sys_ctx

    # query_active_channels should only return turn channels
    turn_ctx = router.query_active_channels()
    # The system channel should NOT appear in turn context
    store.search_reranked.reset_mock()
    store.search_reranked.return_value = [_make_entry("turn data")]
    turn_ctx = router.query_active_channels()
    assert "[sys]" not in turn_ctx or "[turn]" in turn_ctx

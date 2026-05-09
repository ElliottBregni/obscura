"""Tests for vector memory hook optimizations (Change 4).

Change 4: top_k default 5 -> 3, add inject_vector_memory flag
"""
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_hooks() -> Any:
    """Return a simple mock SessionHookManager."""
    hooks = MagicMock()
    hooks.register = MagicMock()
    return hooks


def _make_vector_store(results: list[Any]) -> Any:
    """Return a mock VectorMemoryStore that returns *results* from search_reranked."""
    store = MagicMock()
    store.search_reranked = MagicMock(return_value=results)
    store.set = MagicMock()
    return store


# ---------------------------------------------------------------------------
# Change 4a: top_k default
# ---------------------------------------------------------------------------


def test_register_vector_memory_hooks_default_top_k_is_3() -> None:
    """The top_k parameter default must be 3 (was 5)."""
    from obscura.core.supervisor.vector_memory_hook import register_vector_memory_hooks

    sig = inspect.signature(register_vector_memory_hooks)
    default = sig.parameters["top_k"].default
    assert default == 3, f"Expected top_k default=3, got {default}"


@pytest.mark.asyncio
async def test_inject_hook_passes_top_k_3_to_search() -> None:
    """The inject hook must call search_reranked with top_k=3 by default."""
    from obscura.core.supervisor.vector_memory_hook import register_vector_memory_hooks

    hooks = _make_hooks()
    store = _make_vector_store([])

    register_vector_memory_hooks(hooks, vector_store=store, session_id="s1")

    # Find the inject hook function that was registered.
    inject_call = None
    for call in hooks.register.call_args_list:
        args = call[0]
        kwargs = call[1]
        hook_point = args[0] if args else kwargs.get("hook_point")
        direction = args[1] if len(args) > 1 else kwargs.get("direction")
        if hasattr(hook_point, "value"):
            hp_name = hook_point.value
        else:
            hp_name = str(hook_point)
        if "PRE_BUILD" in hp_name or direction == "before":
            inject_call = call
            break

    assert inject_call is not None, "PRE_BUILD_CONTEXT hook was not registered"
    # Call the registered coroutine directly.
    fn = inject_call[0][3]  # 4th positional arg is the coroutine
    await fn({"prompt": "what is the project structure?"})

    store.search_reranked.assert_called_once()
    call_kwargs = store.search_reranked.call_args
    top_k_used = (
        call_kwargs[1].get("top_k")
        if call_kwargs[1]
        else call_kwargs[0][2]
    )
    assert top_k_used == 3


@pytest.mark.asyncio
async def test_inject_hook_injects_only_top_k_results() -> None:
    """Even if the store returns 5 results, only top_k are passed through."""
    from obscura.core.supervisor.vector_memory_hook import register_vector_memory_hooks

    # Simulate the store honouring top_k internally (we verify the call arg).
    hooks = _make_hooks()

    class _FakeResult:
        def __init__(self, text: str) -> None:
            self.text = text
            self.score = 0.9

    store = _make_vector_store([_FakeResult(f"memory {i}") for i in range(5)])

    register_vector_memory_hooks(hooks, vector_store=store, session_id="s1", top_k=3)

    inject_fn = None
    for call in hooks.register.call_args_list:
        args = call[0]
        direction = args[1] if len(args) > 1 else ""
        if direction == "before":
            inject_fn = args[3]
            break

    assert inject_fn is not None
    ctx: dict[str, Any] = {"prompt": "find relevant memories"}
    await inject_fn(ctx)

    call_kwargs = store.search_reranked.call_args
    top_k_used = call_kwargs[1].get("top_k") if call_kwargs[1] else None
    assert top_k_used == 3


# ---------------------------------------------------------------------------
# Change 4b: inject_vector_memory flag
# ---------------------------------------------------------------------------


def test_inject_vector_memory_false_skips_pre_build_hook() -> None:
    """When inject_vector_memory=False, no PRE_BUILD_CONTEXT hook is registered."""
    from obscura.core.supervisor.vector_memory_hook import register_vector_memory_hooks

    hooks = _make_hooks()
    store = _make_vector_store([])

    register_vector_memory_hooks(
        hooks, vector_store=store, session_id="s1", inject_vector_memory=False
    )

    # Verify none of the register calls used "before" direction.
    for call in hooks.register.call_args_list:
        args = call[0]
        direction = args[1] if len(args) > 1 else call[1].get("direction", "")
        assert direction != "before", (
            "PRE_BUILD_CONTEXT 'before' hook was registered despite inject_vector_memory=False"
        )


def test_inject_vector_memory_false_still_registers_save_hook() -> None:
    """The POST_MODEL_TURN save hook must still be registered even when injection is off."""
    from obscura.core.supervisor.vector_memory_hook import register_vector_memory_hooks

    hooks = _make_hooks()
    store = _make_vector_store([])

    register_vector_memory_hooks(
        hooks, vector_store=store, session_id="s1", inject_vector_memory=False
    )

    assert hooks.register.called, "No hooks were registered at all"
    # At least one 'after' registration must exist (the save hook).
    directions = [
        (call[0][1] if len(call[0]) > 1 else call[1].get("direction", ""))
        for call in hooks.register.call_args_list
    ]
    assert "after" in directions, "POST_MODEL_TURN 'after' save hook was not registered"

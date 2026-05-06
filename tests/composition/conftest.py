"""Shared fixtures for composition tests.

Composition tests focus on the *plumbing*: that build_*_session()
returns a session with the right tools registered, that opt-out works,
and that all surfaces converge on the same plugin tool set. End-to-end
LLM tests live elsewhere — these tests are deliberately fast and
deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from obscura.core.types import ToolSpec


@pytest.fixture
def make_fake_tool() -> Callable[[str], ToolSpec]:
    """Factory for plain ToolSpecs that record their invocations."""

    def _make(name: str = "fake_tool") -> ToolSpec:
        async def _handler(args: dict[str, Any]) -> str:  # noqa: ARG001
            return f"called:{name}"

        return ToolSpec(
            name=name,
            description=f"Fake tool {name}",
            input_schema={"type": "object", "properties": {}},
            handler=_handler,
        )

    return _make

"""Tests for `composition.backend_factory.create_backend`.

The factory is a thin dispatch — verify each backend enum maps to the
right concrete class without exercising auth/MCP. Mock the providers
to avoid network/SDK deps.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from obscura.composition.backend_factory import create_backend
from obscura.core.enums.agent import Backend


@pytest.mark.unit
def test_unknown_backend_raises() -> None:
    fake_auth = MagicMock()

    class _NotABackend:
        value = "imaginary"

    with pytest.raises(ValueError, match="Unknown backend"):
        create_backend(
            backend=_NotABackend(),  # type: ignore[arg-type]
            auth=fake_auth,
            model=None,
            system_prompt="",
            mcp_servers=None,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("enum_member", "class_attr"),
    [
        (Backend.COPILOT, "CopilotBackend"),
        (Backend.CLAUDE, "ClaudeBackend"),
        (Backend.LOCALLLM, "LocalLLMBackend"),
        (Backend.OPENAI, "OpenAIBackend"),
        (Backend.CODEX, "CodexBackend"),
        (Backend.MOONSHOT, "MoonshotBackend"),
    ],
)
def test_dispatches_to_right_class(
    enum_member: Backend,
    class_attr: str,
) -> None:
    """Each Backend member instantiates the matching provider class.

    Patches at the import site (composition.backend_factory) since the
    factory binds names at module load time.
    """
    fake_auth = MagicMock()
    fake_instance = MagicMock()

    with (
        patch(
            f"obscura.composition.backend_factory.{class_attr}",
            return_value=fake_instance,
        ) as mock_cls,
        patch(
            "obscura.composition.backend_factory.wrap_if_enabled",
            side_effect=lambda inst, **_: inst,
        ),
    ):
        result = create_backend(
            backend=enum_member,
            auth=fake_auth,
            model=None,
            system_prompt="",
            mcp_servers=None,
        )

    mock_cls.assert_called_once()
    assert result is fake_instance

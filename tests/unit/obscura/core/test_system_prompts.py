"""Tests for system prompts functionality."""

import pytest

from obscura.core.system_prompts import (
    compose_environment_context,
    compose_system_prompt,
    get_default_system_prompt,
    load_custom_system_prompt,
)


def test_get_default_system_prompt() -> None:
    """Test getting the default Obscura system prompt."""
    prompt = get_default_system_prompt()

    assert len(prompt) > 500
    assert "Obscura Agent" in prompt
    assert "Tool Usage" in prompt


def test_compose_with_default() -> None:
    """Test composing prompt with default included."""
    composed = compose_system_prompt(
        base="You are a helpful assistant.",
        include_default=True,
    )

    assert "Obscura Agent" in composed
    assert "helpful assistant" in composed
    assert len(composed) > len(get_default_system_prompt())


def test_compose_without_default() -> None:
    """Test composing prompt without default."""
    composed = compose_system_prompt(
        base="You are an expert.",
        include_default=False,
    )

    assert "Obscura Agent Runtime" not in composed
    assert "expert" in composed
    assert composed == "You are an expert."


def test_compose_with_custom_sections() -> None:
    """Test composing with additional custom sections."""
    composed = compose_system_prompt(
        base="Base prompt",
        include_default=True,
        custom_sections=["## Custom Section", "Content here"],
    )

    assert "Obscura Agent" in composed
    assert "Base prompt" in composed
    assert "Custom Section" in composed
    assert "Content here" in composed


def test_compose_empty() -> None:
    """Test composing with no base prompt."""
    composed = compose_system_prompt(include_default=True)
    default = get_default_system_prompt()

    # Both should have same content (whitespace normalization may differ slightly)
    assert composed.strip() == default.strip()
    assert "Obscura Agent" in composed


def test_load_custom_system_prompt(tmp_path) -> None:
    """Test loading custom prompt from file."""
    prompt_file = tmp_path / "custom.md"
    prompt_file.write_text("Custom prompt content")

    loaded = load_custom_system_prompt(prompt_file)
    assert loaded == "Custom prompt content"


def test_load_custom_system_prompt_not_found() -> None:
    """Test loading non-existent prompt file."""
    with pytest.raises(FileNotFoundError):
        load_custom_system_prompt("/nonexistent/path.md")


def test_default_prompt_has_tool_usage_section() -> None:
    """Test that default prompt documents tool usage rules."""
    prompt = get_default_system_prompt()

    assert "Tool Usage" in prompt
    assert "ONLY call tools" in prompt or "tool schemas" in prompt


def test_default_prompt_has_delegation_section() -> None:
    """Test that default prompt includes delegation guidance."""
    prompt = get_default_system_prompt()

    assert "Delegation" in prompt or "Sub-Agent" in prompt or "delegate" in prompt.lower()


def test_default_prompt_has_identity() -> None:
    """Test that default prompt establishes agent identity."""
    prompt = get_default_system_prompt()

    assert "Obscura Agent" in prompt
    assert "Identity" in prompt


# ===================================================================
# compose_environment_context
# ===================================================================


def test_compose_environment_context_basic() -> None:
    """All fields provided → template filled correctly."""
    result = compose_environment_context(
        plugin_ids=["websearch", "gitleaks", "system-tools"],
        capabilities=["shell.exec", "file.read"],
        agent_types=["loop", "daemon", "aper"],
        bootstrap_summary="3/3 plugins OK",
    )
    assert "Available Plugins (3)" in result
    assert "- websearch" in result
    assert "- gitleaks" in result
    assert "- system-tools" in result
    assert "- shell.exec" in result
    assert "- file.read" in result
    assert "loop, daemon, aper" in result
    assert "3/3 plugins OK" in result


def test_compose_environment_context_empty() -> None:
    """No args → valid string with sensible defaults."""
    result = compose_environment_context()
    assert "Available Plugins (0)" in result
    assert "None discovered" in result
    assert "None configured" in result
    assert "loop (default)" in result
    assert "All plugins bootstrapped successfully" in result


def test_compose_environment_context_partial() -> None:
    """Only plugin_ids provided, rest defaults."""
    result = compose_environment_context(plugin_ids=["websearch"])
    assert "Available Plugins (1)" in result
    assert "- websearch" in result
    assert "None configured" in result
    assert "loop (default)" in result


def test_environment_context_template_exists() -> None:
    """The environment_context.txt template file must exist."""
    from obscura.core.system_prompts import _PROMPTS_DIR

    template_path = _PROMPTS_DIR / "environment_context.txt"
    assert template_path.exists(), f"Missing: {template_path}"
    content = template_path.read_text(encoding="utf-8")
    assert "{plugin_count}" in content
    assert "{plugin_list}" in content
    assert "{capability_list}" in content
    assert "{agent_types}" in content
    assert "{bootstrap_summary}" in content

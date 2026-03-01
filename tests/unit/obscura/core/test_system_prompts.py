"""Tests for system prompts functionality."""

from pathlib import Path

import pytest

from obscura.core.system_prompts import (
    compose_system_prompt,
    get_default_system_prompt,
    load_custom_system_prompt,
)


def test_get_default_system_prompt():
    """Test getting the default Obscura system prompt."""
    prompt = get_default_system_prompt()
    
    assert len(prompt) > 1000
    assert "Obscura Agent Runtime" in prompt
    assert "web_search" in prompt
    assert "Architecture" in prompt
    assert "obscura/" in prompt  # Codebase structure
    assert "Memory System" in prompt
    assert "Security Guardrails" in prompt


def test_compose_with_default():
    """Test composing prompt with default included."""
    composed = compose_system_prompt(
        base="You are a helpful assistant.",
        include_default=True,
    )
    
    assert "Obscura Agent Runtime" in composed
    assert "helpful assistant" in composed
    assert len(composed) > len(get_default_system_prompt())


def test_compose_without_default():
    """Test composing prompt without default."""
    composed = compose_system_prompt(
        base="You are an expert.",
        include_default=False,
    )
    
    assert "Obscura Agent Runtime" not in composed
    assert "expert" in composed
    assert composed == "You are an expert."


def test_compose_with_custom_sections():
    """Test composing with additional custom sections."""
    composed = compose_system_prompt(
        base="Base prompt",
        include_default=True,
        custom_sections=["## Custom Section", "Content here"],
    )
    
    assert "Obscura Agent Runtime" in composed
    assert "Base prompt" in composed
    assert "Custom Section" in composed
    assert "Content here" in composed


def test_compose_empty():
    """Test composing with no base prompt."""
    composed = compose_system_prompt(include_default=True)
    default = get_default_system_prompt()
    
    # Both should have same content (whitespace normalization may differ slightly)
    assert composed.strip() == default.strip()
    assert "Obscura Agent Runtime" in composed


def test_load_custom_system_prompt(tmp_path):
    """Test loading custom prompt from file."""
    prompt_file = tmp_path / "custom.md"
    prompt_file.write_text("Custom prompt content")
    
    loaded = load_custom_system_prompt(prompt_file)
    assert loaded == "Custom prompt content"


def test_load_custom_system_prompt_not_found():
    """Test loading non-existent prompt file."""
    with pytest.raises(FileNotFoundError):
        load_custom_system_prompt("/nonexistent/path.md")


def test_default_prompt_has_all_tools():
    """Test that default prompt documents all major tool categories."""
    prompt = get_default_system_prompt()
    
    # Tool categories
    assert "web_search" in prompt
    assert "web_fetch" in prompt
    assert "run_shell" in prompt
    assert "run_python3" in prompt
    assert "read_text_file" in prompt
    assert "write_text_file" in prompt
    assert "get_system_info" in prompt
    assert "list_processes" in prompt
    assert "list_listening_ports" in prompt
    assert "signal_process" in prompt
    assert "security_lookup" in prompt
    assert "manage_crontab" in prompt
    assert "task" in prompt


def test_default_prompt_has_codebase_info():
    """Test that default prompt includes Obscura codebase structure."""
    prompt = get_default_system_prompt()
    
    # Key directories
    assert "core/" in prompt
    assert "providers/" in prompt
    assert "tools/" in prompt
    assert "integrations/" in prompt
    assert "agent/" in prompt
    assert "routes/" in prompt


def test_default_prompt_has_best_practices():
    """Test that default prompt includes best practices."""
    prompt = get_default_system_prompt()
    
    assert "Best Practices" in prompt
    assert "proactively" in prompt or "Proactively" in prompt
    assert "Read before" in prompt or "read before" in prompt

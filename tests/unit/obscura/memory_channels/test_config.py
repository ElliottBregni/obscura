"""Tests for obscura.memory_channels.config — TOML parsing."""

from __future__ import annotations

from obscura.memory_channels.config import (
    _parse_channels,
    merge_channels,
)
from obscura.memory_channels.models import ChannelTriggers, MemoryChannel


def test_parse_channel_from_dict():
    raw = {
        "workspace-arch": {
            "namespace": "workspace:architecture",
            "query_template": "architecture for {file_stem}",
            "max_tokens": 500,
            "priority": 80,
            "injection": "turn",
            "file_globs": ["obscura/**/*.py"],
        },
    }
    channels = _parse_channels(raw)
    assert len(channels) == 1
    ch = channels[0]
    assert ch.name == "workspace-arch"
    assert ch.namespace == "workspace:architecture"
    assert ch.triggers.file_globs == ("obscura/**/*.py",)
    assert ch.max_tokens == 500
    assert ch.priority == 80


def test_parse_channel_with_keywords():
    raw = {
        "jira": {
            "namespace": "project:jira",
            "keywords": ["jira", "ticket"],
        },
    }
    channels = _parse_channels(raw)
    assert len(channels) == 1
    assert channels[0].triggers.keywords == ("jira", "ticket")


def test_parse_channel_with_always():
    raw = {
        "prefs": {
            "namespace": "user:prefs",
            "always": True,
            "injection": "system",
        },
    }
    channels = _parse_channels(raw)
    assert len(channels) == 1
    assert channels[0].triggers.always is True
    assert channels[0].injection == "system"


def test_parse_channel_with_nested_triggers():
    raw = {
        "git": {
            "namespace": "git:workflow",
            "triggers": {
                "tool_names": ["git_status", "git_diff"],
                "keywords": ["branch"],
            },
        },
    }
    channels = _parse_channels(raw)
    assert len(channels) == 1
    assert channels[0].triggers.tool_names == ("git_status", "git_diff")
    assert channels[0].triggers.keywords == ("branch",)


def test_parse_defaults_when_missing():
    raw = {"minimal": {"namespace": "ns:min"}}
    channels = _parse_channels(raw)
    assert len(channels) == 1
    ch = channels[0]
    assert ch.query_template == "{query}"
    assert ch.max_tokens == 500
    assert ch.priority == 50
    assert ch.injection == "turn"
    assert ch.enabled is True


def test_merge_agent_overrides():
    global_ch = [
        MemoryChannel(name="jira", namespace="project:jira", triggers=ChannelTriggers()),
        MemoryChannel(name="git", namespace="git:workflow", triggers=ChannelTriggers()),
    ]
    agent_ch = [
        MemoryChannel(name="jira", namespace="project:jira-custom", triggers=ChannelTriggers()),
        MemoryChannel(name="review", namespace="review:patterns", triggers=ChannelTriggers()),
    ]
    merged = merge_channels(global_ch, agent_ch)
    by_name = {c.name: c for c in merged}
    assert by_name["jira"].namespace == "project:jira-custom"  # agent override
    assert by_name["git"].namespace == "git:workflow"  # global preserved
    assert by_name["review"].namespace == "review:patterns"  # agent addition


def test_parse_malformed_channel_skipped():
    raw = {
        "good": {"namespace": "ns:good"},
        "bad": "not a dict",
    }
    channels = _parse_channels(raw)
    assert len(channels) == 1
    assert channels[0].name == "good"

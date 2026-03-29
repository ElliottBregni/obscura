"""Tests for obscura.memory_channels.classifier — turn auto-classification."""

from __future__ import annotations

from obscura.memory_channels.classifier import TurnClassifier
from obscura.memory_channels.models import ChannelTriggers, MemoryChannel


def _ch(name, namespace, **trigger_kwargs):
    return MemoryChannel(
        name=name, namespace=namespace,
        triggers=ChannelTriggers(**trigger_kwargs),
    )


def test_classify_jira_keywords():
    channels = [_ch("jira", "project:jira", keywords=("jira", "ticket", "PROJ-"))]
    classifier = TurnClassifier(channels)
    namespaces = classifier.classify("Check the PROJ-123 ticket", "Looking into it.")
    assert "project:jira" in namespaces
    assert "cli:conversation" in namespaces  # default always present


def test_classify_file_path():
    channels = [_ch("arch", "workspace:arch", file_globs=("obscura/**/*.py",))]
    classifier = TurnClassifier(channels)
    namespaces = classifier.classify(
        "Edit obscura/providers/copilot.py", "Done, updated the streaming logic."
    )
    assert "workspace:arch" in namespaces


def test_classify_no_match_falls_back():
    channels = [_ch("jira", "project:jira", keywords=("jira",))]
    classifier = TurnClassifier(channels)
    namespaces = classifier.classify("Hello world", "Hi there!")
    assert namespaces == ["cli:conversation"]


def test_classify_multiple_namespaces():
    channels = [
        _ch("jira", "project:jira", keywords=("jira",)),
        _ch("git", "git:workflow", keywords=("commit", "branch")),
    ]
    classifier = TurnClassifier(channels)
    namespaces = classifier.classify(
        "Commit the jira fix to a new branch", "Created branch fix/PROJ-123."
    )
    assert "project:jira" in namespaces
    assert "git:workflow" in namespaces
    assert "cli:conversation" in namespaces


def test_classify_always_channel():
    channels = [_ch("prefs", "user:prefs", always=True)]
    classifier = TurnClassifier(channels)
    namespaces = classifier.classify("random text", "response")
    assert "user:prefs" in namespaces


def test_classify_disabled_channel_skipped():
    channel = MemoryChannel(
        name="disabled", namespace="ns:disabled",
        triggers=ChannelTriggers(keywords=("match",)),
        enabled=False,
    )
    classifier = TurnClassifier([channel])
    namespaces = classifier.classify("this should match", "response")
    assert "ns:disabled" not in namespaces

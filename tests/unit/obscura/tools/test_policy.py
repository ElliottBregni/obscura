"""Tests for obscura.tools.policy — tool access control."""

from __future__ import annotations

from pathlib import Path

from obscura.tools.policy.engine import evaluate_policy
from obscura.tools.policy.models import PolicyResult, ToolPolicy


class TestToolPolicy:
    """ToolPolicy dataclass behaviour."""

    def test_frozen(self) -> None:
        p = ToolPolicy(name="default")
        assert p.name == "default"
        assert p.allow_list == frozenset()
        assert p.deny_list == frozenset()
        assert p.base_dir is None
        assert p.full_access is False

    def test_with_lists(self) -> None:
        p = ToolPolicy(
            name="restricted",
            allow_list=frozenset({"read_file", "search_files"}),
            deny_list=frozenset({"delete_file"}),
        )
        assert "read_file" in p.allow_list
        assert "delete_file" in p.deny_list


class TestPolicyResult:
    """PolicyResult dataclass."""

    def test_allowed(self) -> None:
        r = PolicyResult(allowed=True, reason="ok")
        assert r.allowed is True
        assert r.reason == "ok"

    def test_denied(self) -> None:
        r = PolicyResult(allowed=False, reason="nope")
        assert r.allowed is False


class TestEvaluatePolicy:
    """evaluate_policy() logic."""

    def test_full_access_allows_everything(self) -> None:
        policy = ToolPolicy(name="admin", full_access=True)
        result = evaluate_policy(policy, "anything")
        assert result.allowed is True
        assert "full_access" in result.reason

    def test_deny_list_blocks(self) -> None:
        policy = ToolPolicy(name="safe", deny_list=frozenset({"delete_file"}))
        result = evaluate_policy(policy, "delete_file")
        assert result.allowed is False
        assert "deny_list" in result.reason

    def test_deny_list_allows_others(self) -> None:
        policy = ToolPolicy(name="safe", deny_list=frozenset({"delete_file"}))
        result = evaluate_policy(policy, "read_file")
        assert result.allowed is True

    def test_allow_list_permits_listed(self) -> None:
        policy = ToolPolicy(
            name="readonly",
            allow_list=frozenset({"read_file", "search_files"}),
        )
        result = evaluate_policy(policy, "read_file")
        assert result.allowed is True

    def test_allow_list_denies_unlisted(self) -> None:
        policy = ToolPolicy(
            name="readonly",
            allow_list=frozenset({"read_file"}),
        )
        result = evaluate_policy(policy, "write_file")
        assert result.allowed is False
        assert "allow_list" in result.reason

    def test_empty_allow_list_permits_all(self) -> None:
        policy = ToolPolicy(name="open")
        result = evaluate_policy(policy, "anything")
        assert result.allowed is True

    def test_base_dir_allows_within(self, tmp_path: Path) -> None:
        policy = ToolPolicy(name="sandboxed", base_dir=tmp_path)
        child = tmp_path / "subdir" / "file.txt"
        result = evaluate_policy(policy, "read_file", {"path": str(child)})
        assert result.allowed is True

    def test_base_dir_blocks_escape(self, tmp_path: Path) -> None:
        policy = ToolPolicy(name="sandboxed", base_dir=tmp_path)
        result = evaluate_policy(policy, "read_file", {"path": "/etc/passwd"})
        assert result.allowed is False
        assert "escapes base_dir" in result.reason

    def test_base_dir_ignores_non_fs_tools(self, tmp_path: Path) -> None:
        policy = ToolPolicy(name="sandboxed", base_dir=tmp_path)
        result = evaluate_policy(policy, "web_search", {"path": "/etc/passwd"})
        assert result.allowed is True

    def test_deny_overrides_allow(self) -> None:
        policy = ToolPolicy(
            name="mixed",
            allow_list=frozenset({"read_file", "delete_file"}),
            deny_list=frozenset({"delete_file"}),
        )
        result = evaluate_policy(policy, "delete_file")
        assert result.allowed is False
        assert "deny_list" in result.reason

    def test_full_access_overrides_deny(self) -> None:
        policy = ToolPolicy(
            name="override",
            full_access=True,
            deny_list=frozenset({"delete_file"}),
        )
        result = evaluate_policy(policy, "delete_file")
        assert result.allowed is True

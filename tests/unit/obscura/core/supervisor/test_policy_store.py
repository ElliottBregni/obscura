"""Tests for immutable policy versioning."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.supervisor.policy_store import PolicyStore


@pytest.fixture
def store(tmp_path: Path) -> PolicyStore:
    s = PolicyStore(tmp_path / "test.db")
    yield s
    s.close()


class TestPolicyStore:
    def test_create_global_policy(self, store: PolicyStore) -> None:
        policy = store.create_version(
            scope="global",
            policy_json={
                "tool_allowlist": None,
                "tool_denylist": ["dangerous_tool"],
                "require_confirmation": ["bash"],
                "max_turns": 15,
                "token_budget": 100000,
                "allow_dynamic_tools": True,
            },
        )
        assert policy.version == 1
        assert policy.scope == "global"
        assert policy.tool_denylist == ["dangerous_tool"]
        assert policy.require_confirmation == ["bash"]
        assert policy.max_turns == 15
        assert policy.allow_dynamic_tools is True

    def test_versions_increment(self, store: PolicyStore) -> None:
        v1 = store.create_version(policy_json={"max_turns": 10})
        v2 = store.create_version(policy_json={"max_turns": 20})
        assert v1.version == 1
        assert v2.version == 2

    def test_versions_immutable(self, store: PolicyStore) -> None:
        v1 = store.create_version(policy_json={"max_turns": 10})
        loaded = store.get_version(v1.policy_id)
        assert loaded is not None
        assert loaded.max_turns == 10

    def test_scoped_policies(self, store: PolicyStore) -> None:
        global_p = store.create_version(scope="global", policy_json={"max_turns": 10})
        agent_p = store.create_version(
            scope="agent", scope_id="agent-1", policy_json={"max_turns": 5}
        )
        session_p = store.create_version(
            scope="session", scope_id="sess-1", policy_json={"max_turns": 3}
        )

        assert global_p.scope == "global"
        assert agent_p.scope == "agent"
        assert session_p.scope == "session"

    def test_get_latest(self, store: PolicyStore) -> None:
        store.create_version(policy_json={"max_turns": 10})
        store.create_version(policy_json={"max_turns": 20})
        latest = store.get_latest()
        assert latest is not None
        assert latest.version == 2
        assert latest.max_turns == 20

    def test_list_versions(self, store: PolicyStore) -> None:
        store.create_version(policy_json={"v": 1})
        store.create_version(policy_json={"v": 2})
        store.create_version(policy_json={"v": 3})
        versions = store.list_versions()
        assert len(versions) == 3
        assert versions[0].version == 3  # newest first

    def test_deterministic_hash(self, store: PolicyStore) -> None:
        v1 = store.create_version(policy_json={"max_turns": 10, "token_budget": 5000})
        v2 = store.create_version(policy_json={"max_turns": 10, "token_budget": 5000})
        assert v1.hash == v2.hash

    def test_hash_changes_with_content(self, store: PolicyStore) -> None:
        v1 = store.create_version(policy_json={"max_turns": 10})
        v2 = store.create_version(policy_json={"max_turns": 20})
        assert v1.hash != v2.hash

    def test_get_latest_nonexistent_returns_none(self, store: PolicyStore) -> None:
        result = store.get_latest(scope="agent", scope_id="nonexistent")
        assert result is None

    def test_tool_allowlist_none_means_all(self, store: PolicyStore) -> None:
        policy = store.create_version(policy_json={"tool_allowlist": None})
        assert policy.tool_allowlist is None

    def test_defaults(self, store: PolicyStore) -> None:
        policy = store.create_version(policy_json={})
        assert policy.max_turns == 10
        assert policy.token_budget == 0
        assert policy.allow_dynamic_tools is False
        assert policy.tool_denylist == []
        assert policy.require_confirmation == []

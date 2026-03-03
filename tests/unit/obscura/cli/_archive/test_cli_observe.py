"""Tests for obscura.cli observe command helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from obscura.auth.models import AuthenticatedUser
from obscura.cli import (
    collect_observed_agent_states,
    find_stale_agent_ids,
    run_observe,
    build_parser,
)
from obscura.memory import MemoryStore


def _user(user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        email=f"{user_id}@obscura.local",
        roles=("operator",),
        org_id="org-test",
        token_type="user",
        raw_token="test-token",
    )


def _configure_memory_dir(tmp_path: Path) -> None:
    import os

    os.environ["OBSCURA_MEMORY_DIR"] = str(tmp_path)
    MemoryStore.reset_instances()


def test_build_parser_observe_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["observe", "--user-id", "builder-claude-user"])
    assert args.command == "observe"
    assert args.namespace == "agent:runtime"
    assert args.interval_seconds == 1.0
    assert args.stale_seconds == 20.0


def test_collect_observed_agent_states_and_stale_detection(tmp_path: Path) -> None:
    _configure_memory_dir(tmp_path)
    user = _user("observe-u1")
    store = MemoryStore.for_user(user)
    now = datetime.now(UTC)

    store.set(
        "agent_state_agent-1",
        {
            "agent_id": "agent-1",
            "name": "alpha",
            "status": "RUNNING",
            "updated_at": (now - timedelta(seconds=45)).isoformat(),
            "iteration_count": 2,
            "error_message": None,
        },
        namespace="agent:runtime",
    )
    store.set(
        "agent_state_agent-2",
        {
            "agent_id": "agent-2",
            "name": "beta",
            "status": "COMPLETED",
            "updated_at": now.isoformat(),
            "iteration_count": 4,
            "error_message": None,
        },
        namespace="agent:runtime",
    )
    store.set(
        "not_an_agent_state",
        {"value": "ignored"},
        namespace="agent:runtime",
    )

    states = collect_observed_agent_states(store, namespace="agent:runtime")
    assert [state.agent_id for state in states] == ["agent-1", "agent-2"]

    stale_ids = find_stale_agent_ids(states, now=now, stale_seconds=20.0)
    assert stale_ids == ["agent-1"]


def test_run_observe_once_prints_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure_memory_dir(tmp_path)
    user = _user("observe-u2")
    store = MemoryStore.for_user(user)
    now = datetime.now(UTC)
    store.set(
        "agent_state_agent-xyz",
        {
            "agent_id": "agent-xyz",
            "name": "watch-agent",
            "status": "WAITING",
            "updated_at": now.isoformat(),
            "iteration_count": 1,
            "error_message": None,
        },
        namespace="agent:runtime",
    )

    parser = build_parser()
    args = parser.parse_args(["observe", "--user-id", user.user_id, "--once"])
    exit_code = run_observe(args)
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "[observe] user=observe-u2" in output
    assert "agent-xyz name=watch-agent status=WAITING" in output

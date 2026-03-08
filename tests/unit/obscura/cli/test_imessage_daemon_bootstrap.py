from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import obscura.cli as cli_mod


@pytest.mark.asyncio
async def test_start_imessage_daemon_preserves_trigger_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / ".obscura"
    cfg_path.mkdir(parents=True, exist_ok=True)
    (cfg_path / "agents.yaml").write_text("agents: []", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    trig = SimpleNamespace(
        imessage={
            "contacts": [],
            "poll_interval": 2,
            "forced_recipient": "+12316333624",
        },
        notify_user=True,
        priority="high",
    )
    agent_def = SimpleNamespace(
        type="daemon",
        name="imessage-assistant",
        model="codex",
        system_prompt="test",
        triggers=[trig],
    )
    cfg = SimpleNamespace(agents=[agent_def])

    monkeypatch.setattr(
        "obscura.agent.supervisor.SupervisorConfig.from_yaml",
        lambda _p: cfg,
    )

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    monkeypatch.setattr(cli_mod, "ObscuraClient", lambda *a, **k: fake_client)

    captured: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, _client: object, name: str, triggers: list[object]) -> None:
            captured["name"] = name
            captured["triggers"] = triggers

        async def loop_forever(self) -> None:
            return

    monkeypatch.setattr("obscura.agent.daemon_agent.DaemonAgent", FakeDaemon)

    task = await cli_mod._start_imessage_daemon(AsyncMock())
    assert task is not None
    await task

    triggers = captured["triggers"]
    assert isinstance(triggers, list)
    assert len(triggers) == 1
    trigger0 = triggers[0]
    assert getattr(trigger0, "data").get("forced_recipient") == "+12316333624"

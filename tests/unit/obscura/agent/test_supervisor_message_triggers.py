"""Tests for manifest/yaml message trigger configuration."""

from __future__ import annotations

from pathlib import Path

from obscura.agent.supervisor import SupervisorConfig


def test_from_yaml_parses_message_trigger(tmp_path: Path) -> None:
    cfg = tmp_path / "agents.yaml"
    cfg.write_text(
        (
            "agents:\n"
            "  - name: msg\n"
            "    type: daemon\n"
            "    model: copilot\n"
            "    triggers:\n"
            "      - description: inbound\n"
            "        message:\n"
            "          platform: imessage\n"
            "          contacts: ['+15551234567']\n"
            "          poll_interval: 25\n"
            "          account_id: default\n"
        ),
        encoding="utf-8",
    )

    loaded = SupervisorConfig.from_yaml(cfg)
    assert len(loaded.agents) == 1
    assert len(loaded.agents[0].triggers) == 1
    trig = loaded.agents[0].triggers[0]
    assert trig.message is not None
    assert trig.message["platform"] == "imessage"
    assert trig.message["poll_interval"] == 25


def test_from_directory_parses_message_trigger(tmp_path: Path) -> None:
    (tmp_path / "msg.agent.md").write_text(
        (
            "---\n"
            "name: msg\n"
            "agent-type: daemon\n"
            "triggers:\n"
            "  - description: inbound\n"
            "    message:\n"
            "      platform: imessage\n"
            "      contacts:\n"
            "        - '+15551234567'\n"
            "      poll_interval: 20\n"
            "      account_id: default\n"
            "---\n"
            "You handle messages.\n"
        ),
        encoding="utf-8",
    )

    loaded = SupervisorConfig.from_directory(tmp_path)
    assert len(loaded.agents) == 1
    assert len(loaded.agents[0].triggers) == 1
    trig = loaded.agents[0].triggers[0]
    assert trig.message is not None
    assert trig.message["platform"] == "imessage"
    assert trig.message["poll_interval"] == 20

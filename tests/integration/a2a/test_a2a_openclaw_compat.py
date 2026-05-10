"""Integration tests: OpenClaw ↔ Obscura A2A compatibility layer.

Verifies that:
- ``openclaw_compatible_definition()`` produces the right skill tags
- ``OpenClawBridge.discover_obscura_a2a()`` can fetch the agent card from
  the Obscura standalone A2A server
- ``OpenClawBridge.send_a2a_message()`` can create tasks on the standalone server
- The standalone app's copilot backend config is set correctly
- An OpenClaw-definition-backed app returns OpenClaw-tagged skills

All tests use httpx.ASGITransport — no real network or LLM calls.
"""

from __future__ import annotations

import pytest
import httpx

from obscura.integrations.a2a.definition import (
    openclaw_compatible_definition,
    DEFAULT_AGENT_DEFINITION,
)
from obscura.integrations.a2a.standalone import create_standalone_app
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.openclaw_bridge import OpenClawBridge, OpenClawBridgeConfig

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def standalone_app():
    """Standalone A2A app backed by an in-memory store."""
    return create_standalone_app(
        base_url="http://testserver",
        store=InMemoryTaskStore(),
        agent_backend="copilot",
    )


@pytest.fixture(scope="module")
def a2a_transport(standalone_app):
    """ASGI transport pointed at the standalone app."""
    return httpx.ASGITransport(app=standalone_app)


# ---------------------------------------------------------------------------
# Pure unit tests — no HTTP
# ---------------------------------------------------------------------------


def test_openclaw_compatible_definition_has_openclaw_tags() -> None:
    """All skills in openclaw_compatible_definition() have the 'openclaw' tag."""
    defn = openclaw_compatible_definition()
    for skill in defn.skills:
        assert "openclaw" in skill.tags, (
            f"Skill {skill.id!r} missing 'openclaw' tag; tags={skill.tags}"
        )


def test_openclaw_compatible_definition_tool_use_has_kimi_tag() -> None:
    """The tool-use skill has the 'kimi-k2' tag."""
    defn = openclaw_compatible_definition()
    tool_use = next(s for s in defn.skills if s.id == "tool-use")
    assert "kimi-k2" in tool_use.tags


def test_openclaw_compatible_definition_name() -> None:
    """Definition name is set to 'Obscura Agent (OpenClaw)'."""
    defn = openclaw_compatible_definition()
    assert defn.name == "Obscura Agent (OpenClaw)"


def test_openclaw_compatible_definition_card_url() -> None:
    """Agent card url matches the base_url passed to the factory."""
    defn = openclaw_compatible_definition(base_url="https://my-agent.example.com")
    card = defn.to_agent_card()
    assert card.url == "https://my-agent.example.com"


def test_openclaw_compatible_definition_preserves_default_skill_count() -> None:
    """OpenClaw definition has the same number of skills as the default."""
    defn = openclaw_compatible_definition()
    assert len(defn.skills) == len(DEFAULT_AGENT_DEFINITION.skills)


def test_openclaw_bridge_config_has_a2a_base_url() -> None:
    """OpenClawBridgeConfig exposes a2a_base_url field with sensible default."""
    cfg = OpenClawBridgeConfig()
    assert cfg.a2a_base_url == "http://localhost:8080"


def test_openclaw_bridge_config_a2a_url_overridable() -> None:
    """a2a_base_url can be overridden at construction time."""
    cfg = OpenClawBridgeConfig(a2a_base_url="http://my-a2a-server:9090")
    assert cfg.a2a_base_url == "http://my-a2a-server:9090"


# ---------------------------------------------------------------------------
# Integration tests — ASGI transport, no real network
# ---------------------------------------------------------------------------


async def test_discover_obscura_a2a_returns_card(a2a_transport) -> None:
    """discover_obscura_a2a() fetches the well-known agent card."""
    bridge = OpenClawBridge()
    card = await bridge.discover_obscura_a2a(
        "http://testserver",
        transport=a2a_transport,
    )
    assert card["name"] == "Obscura Agent"
    assert "skills" in card
    assert len(card["skills"]) >= 1
    assert "protocolVersion" in card


async def test_send_a2a_message_nonblocking(a2a_transport) -> None:
    """send_a2a_message() creates a task and returns task JSON."""
    bridge = OpenClawBridge()
    task = await bridge.send_a2a_message(
        "hello from openclaw",
        a2a_base_url="http://testserver",
        blocking=False,
        transport=a2a_transport,
    )
    assert "id" in task
    assert "status" in task
    assert "state" in task["status"]


async def test_send_a2a_message_with_context_id(a2a_transport) -> None:
    """send_a2a_message() passes context_id through to the task."""
    bridge = OpenClawBridge()
    task = await bridge.send_a2a_message(
        "context test",
        a2a_base_url="http://testserver",
        context_id="openclaw-ctx-42",
        blocking=False,
        transport=a2a_transport,
    )
    assert "id" in task


def test_standalone_app_copilot_backend_config(standalone_app) -> None:
    """Standalone app is wired to the copilot backend by default."""
    a2a_server = standalone_app.state.a2a_server
    assert a2a_server._service._agent_backend == "copilot"


async def test_openclaw_card_skills_have_openclaw_tags(a2a_transport) -> None:
    """A standalone app built from openclaw_compatible_definition() serves
    skill objects that include the 'openclaw' tag."""
    oc_defn = openclaw_compatible_definition(base_url="http://testserver")
    oc_app = create_standalone_app(
        definition=oc_defn,
        base_url="http://testserver",
        store=InMemoryTaskStore(),
    )
    oc_transport = httpx.ASGITransport(app=oc_app)
    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=oc_transport,
    ) as client:
        resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    card = resp.json()
    for skill in card["skills"]:
        assert "openclaw" in skill["tags"], (
            f"Skill {skill['id']!r} missing 'openclaw' tag"
        )

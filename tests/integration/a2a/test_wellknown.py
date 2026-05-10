"""Integration tests: /.well-known/agent.json and /a2a/v1/agent endpoints.

Covers A2A agent-discovery requirements:
- HTTP 200 on GET /.well-known/agent.json
- JSON content-type
- Required A2A protocol fields (name, url, protocolVersion, capabilities)
- Bearer security scheme declaration
- /a2a/v1/agent returns the same card as /.well-known/agent.json
"""

from __future__ import annotations

import pytest

TEST_AGENT_NAME = "integration-test-agent"
TEST_AGENT_URL = "http://testserver"

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_wellknown_returns_200(a2a_http) -> None:
    """GET /.well-known/agent.json must return HTTP 200."""
    resp = await a2a_http.get("/.well-known/agent.json")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_wellknown_content_type_json(a2a_http) -> None:
    """Response must be application/json."""
    resp = await a2a_http.get("/.well-known/agent.json")
    assert "application/json" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_wellknown_has_required_fields(a2a_http) -> None:
    """Agent card must include all fields required by A2A spec v0.3."""
    resp = await a2a_http.get("/.well-known/agent.json")
    card = resp.json()

    assert card["name"] == TEST_AGENT_NAME
    assert card["url"] == TEST_AGENT_URL
    assert card["protocolVersion"] == "0.3"
    assert "capabilities" in card
    assert "securitySchemes" in card
    assert "skills" in card


@pytest.mark.asyncio
async def test_wellknown_capabilities_streaming(a2a_http) -> None:
    """Agent card must declare streaming=True."""
    resp = await a2a_http.get("/.well-known/agent.json")
    caps = resp.json()["capabilities"]
    assert caps["streaming"] is True


@pytest.mark.asyncio
async def test_wellknown_bearer_auth_scheme(a2a_http) -> None:
    """Agent card must declare a bearer HTTP security scheme."""
    resp = await a2a_http.get("/.well-known/agent.json")
    schemes = resp.json()["securitySchemes"]
    assert "bearer" in schemes
    assert schemes["bearer"]["type"] == "http"
    assert schemes["bearer"]["scheme"] == "bearer"


@pytest.mark.asyncio
async def test_wellknown_provider_present(a2a_http) -> None:
    """Agent card should include provider metadata when set."""
    resp = await a2a_http.get("/.well-known/agent.json")
    card = resp.json()
    assert "provider" in card
    assert card["provider"]["name"] == "Obscura"


@pytest.mark.asyncio
async def test_rest_agent_endpoint_matches_wellknown(a2a_http) -> None:
    """/a2a/v1/agent and /.well-known/agent.json must return identical cards."""
    wk = (await a2a_http.get("/.well-known/agent.json")).json()
    rest = (await a2a_http.get("/a2a/v1/agent")).json()
    assert wk == rest


@pytest.mark.asyncio
async def test_agent_card_rest_returns_200(a2a_http) -> None:
    """GET /a2a/v1/agent must return HTTP 200."""
    resp = await a2a_http.get("/a2a/v1/agent")
    assert resp.status_code == 200

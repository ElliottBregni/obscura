"""Tests for Pydantic template schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from obscura.schemas.templates import (
    A2ARemoteToolsSpecSchema,
    APERProfileSchema,
    MCPServerSpecSchema,
    SkillSpecSchema,
    SpawnFromTemplateRequest,
    TemplateCreateRequest,
    TemplateUpdateRequest,
)


class TestAPERProfileSchema:
    def test_defaults(self) -> None:
        p = APERProfileSchema()
        assert "Analyze" in p.analyze_template
        assert "plan" in p.plan_template.lower()
        assert "{goal}" in p.execute_template
        assert p.max_turns == 8

    def test_custom_values(self) -> None:
        p = APERProfileSchema(
            analyze_template="custom analyze",
            plan_template="custom plan",
            execute_template="do {goal}",
            respond_template="done",
            max_turns=3,
        )
        assert p.analyze_template == "custom analyze"
        assert p.max_turns == 3

    def test_max_turns_validation(self) -> None:
        with pytest.raises(ValidationError):
            APERProfileSchema(max_turns=0)
        with pytest.raises(ValidationError):
            APERProfileSchema(max_turns=101)


class TestSkillSpecSchema:
    def test_valid(self) -> None:
        s = SkillSpecSchema(name="test", content="some content")
        assert s.source == "inline"

    def test_name_required(self) -> None:
        with pytest.raises(ValidationError):
            SkillSpecSchema(name="", content="x")

    def test_content_required(self) -> None:
        with pytest.raises(ValidationError):
            SkillSpecSchema(name="x", content="")


class TestMCPServerSpecSchema:
    def test_stdio(self) -> None:
        s = MCPServerSpecSchema(name="pw", transport="stdio", command="npx", args=["-y", "@playwright/mcp"])
        assert s.transport == "stdio"
        assert s.url == ""

    def test_sse(self) -> None:
        s = MCPServerSpecSchema(name="remote", transport="sse", url="http://localhost:3000")
        assert s.transport == "sse"
        assert s.command == ""

    def test_invalid_transport(self) -> None:
        with pytest.raises(ValidationError):
            MCPServerSpecSchema(name="x", transport="websocket")  # type: ignore[arg-type]


class TestA2ARemoteToolsSpecSchema:
    def test_defaults(self) -> None:
        a = A2ARemoteToolsSpecSchema()
        assert a.enabled is True
        assert a.urls == []
        assert a.auth_token is None

    def test_with_token(self) -> None:
        a = A2ARemoteToolsSpecSchema(urls=["http://peer:8080"], auth_token="secret")
        assert a.auth_token == "secret"


class TestTemplateCreateRequest:
    def test_defaults(self) -> None:
        r = TemplateCreateRequest()
        assert r.name == "unnamed-template"
        assert r.model == "claude"
        assert r.aper_profile is None
        assert r.skills == []
        assert r.mcp_servers == []
        assert r.persist is False

    def test_full_payload(self) -> None:
        r = TemplateCreateRequest(
            name="researcher",
            model="claude",
            aper_profile=APERProfileSchema(max_turns=5),
            skills=[SkillSpecSchema(name="s1", content="c1")],
            mcp_servers=[MCPServerSpecSchema(name="pw", transport="stdio", command="npx")],
            a2a_remote_tools=A2ARemoteToolsSpecSchema(urls=["http://peer"]),
            persist=True,
        )
        assert r.aper_profile is not None
        assert r.aper_profile.max_turns == 5
        assert len(r.skills) == 1
        assert len(r.mcp_servers) == 1
        assert r.persist is True

    def test_validation_errors(self) -> None:
        with pytest.raises(ValidationError):
            TemplateCreateRequest(timeout_seconds=-1)
        with pytest.raises(ValidationError):
            TemplateCreateRequest(max_iterations=0)


class TestTemplateUpdateRequest:
    def test_partial(self) -> None:
        r = TemplateUpdateRequest(name="new-name")
        dumped = r.model_dump(exclude_none=True)
        assert dumped == {"name": "new-name"}

    def test_all_none(self) -> None:
        r = TemplateUpdateRequest()
        dumped = r.model_dump(exclude_none=True)
        assert dumped == {}


class TestSpawnFromTemplateRequest:
    def test_defaults(self) -> None:
        r = SpawnFromTemplateRequest(template_id="abc")
        assert r.mode == "loop"
        assert r.name is None
        assert r.prompt == ""

    def test_aper_mode(self) -> None:
        r = SpawnFromTemplateRequest(template_id="abc", mode="aper", prompt="do stuff")
        assert r.mode == "aper"
        assert r.prompt == "do stuff"

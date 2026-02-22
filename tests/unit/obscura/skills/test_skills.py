"""Tests for the skills framework: base, registry, loader, docs_loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, override

import pytest

from obscura.skills.base import (
    CapabilityNotFoundError,
    CapabilityParameter,
    CapabilityReturn,
    CapabilityType,
    Skill,
    SkillCapability,
    SkillError,
    SkillExecutionError,
    SkillHealth,
    SkillInitializationError,
    SkillMetadata,
    SkillNotFoundError,
)
from obscura.skills.docs_loader import (
    MarkdownSkillDocument,
    load_markdown_skill_documents,
    load_markdown_skill_texts,
)
from obscura.skills.loader import SkillLoader
from obscura.skills.registry import (
    SkillRegistry,
    get_global_registry,
    reset_global_registry,
)


# ---------------------------------------------------------------------------
# Concrete skill for testing
# ---------------------------------------------------------------------------


class EchoSkill(Skill):
    """Minimal skill that echoes back input."""

    name = "echo"
    version = "1.0.0"
    description = "Echoes back input"
    metadata = SkillMetadata(author="test", tags=["test", "echo"])

    capabilities = [
        SkillCapability(
            name="echo",
            description="Echo input text",
            parameters=[
                CapabilityParameter("text", "string", "Text to echo"),
            ],
            returns=CapabilityReturn("string", "Echoed text"),
            capability_type=CapabilityType.QUERY,
        ),
        SkillCapability(
            name="count",
            description="Count characters",
            parameters=[
                CapabilityParameter("text", "string", "Text to count"),
                CapabilityParameter("uppercase", "boolean", "Uppercase output", required=False, default=False),
            ],
            returns=CapabilityReturn("number", "Character count"),
        ),
    ]

    @override
    async def initialize(self, config: dict[str, Any]) -> None:
        self._config = config

    @override
    async def execute(self, capability: str, params: dict[str, Any]) -> Any:
        if capability == "echo":
            return params["text"]
        if capability == "count":
            return len(params["text"])
        raise SkillExecutionError(f"Unknown capability: {capability}")

    @override
    async def health_check(self) -> SkillHealth:
        return SkillHealth(healthy=True, message="OK")

    @override
    async def shutdown(self) -> None:
        pass


class StreamSkill(Skill):
    """Skill with streaming support."""

    name = "streamer"
    version = "0.1.0"
    description = "Streams chunks"
    metadata = SkillMetadata(author="test")
    capabilities = [
        SkillCapability(
            name="words",
            description="Stream words",
            parameters=[CapabilityParameter("sentence", "string", "Sentence to split")],
            returns=CapabilityReturn("string", "Word chunks"),
            capability_type=CapabilityType.STREAM,
        ),
    ]

    @override
    async def initialize(self, config: dict[str, Any]) -> None:
        pass

    @override
    async def execute(self, capability: str, params: dict[str, Any]) -> Any:
        return params["sentence"].split()

    @override
    async def execute_stream(
        self, capability: str, params: dict[str, Any]
    ) -> AsyncIterator[Any]:
        for word in params["sentence"].split():
            yield word

    @override
    async def health_check(self) -> SkillHealth:
        return SkillHealth(healthy=True, message="streaming")

    @override
    async def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests: base.py
# ---------------------------------------------------------------------------


class TestCapabilityType:
    def test_values(self) -> None:
        assert CapabilityType.QUERY.value == "query"
        assert CapabilityType.ACTION.value == "action"
        assert CapabilityType.STREAM.value == "stream"
        assert CapabilityType.HYBRID.value == "hybrid"


class TestSkillCapability:
    def test_to_dict(self) -> None:
        cap = SkillCapability(
            name="search",
            description="Search things",
            parameters=[CapabilityParameter("q", "string", "Query")],
            returns=CapabilityReturn("object", "Results", schema={"type": "array"}),
            capability_type=CapabilityType.QUERY,
            examples=[{"q": "hello"}],
        )
        d = cap.to_dict()
        assert d["name"] == "search"
        assert d["type"] == "query"
        assert len(d["parameters"]) == 1
        assert d["parameters"][0]["name"] == "q"
        assert d["returns"]["schema"] == {"type": "array"}
        assert d["examples"] == [{"q": "hello"}]


class TestSkillHealth:
    def test_to_dict(self) -> None:
        h = SkillHealth(healthy=True, message="ok", latency_ms=1.5, details={"x": 1})
        d = h.to_dict()
        assert d["healthy"] is True
        assert d["latency_ms"] == 1.5
        assert d["details"] == {"x": 1}


class TestSkill:
    def test_get_capability(self) -> None:
        skill = EchoSkill()
        assert skill.get_capability("echo") is not None
        assert skill.get_capability("nonexistent") is None

    def test_list_capabilities(self) -> None:
        skill = EchoSkill()
        caps = skill.list_capabilities()
        assert len(caps) == 2
        assert caps[0].name == "echo"

    def test_to_dict(self) -> None:
        skill = EchoSkill()
        d = skill.to_dict()
        assert d["name"] == "echo"
        assert d["version"] == "1.0.0"
        assert len(d["capabilities"]) == 2
        assert d["metadata"]["author"] == "test"

    def test_validate_params_ok(self) -> None:
        skill = EchoSkill()
        errors = skill.validate_params("echo", {"text": "hello"})
        assert errors == []

    def test_validate_params_missing_required(self) -> None:
        skill = EchoSkill()
        errors = skill.validate_params("echo", {})
        assert any("Missing required" in e for e in errors)

    def test_validate_params_unknown_param(self) -> None:
        skill = EchoSkill()
        errors = skill.validate_params("echo", {"text": "hi", "extra": 1})
        assert any("Unknown parameter" in e for e in errors)

    def test_validate_params_wrong_type(self) -> None:
        skill = EchoSkill()
        errors = skill.validate_params("echo", {"text": 123})
        assert any("must be a string" in e for e in errors)

    def test_validate_params_unknown_capability(self) -> None:
        skill = EchoSkill()
        errors = skill.validate_params("nope", {})
        assert any("Unknown capability" in e for e in errors)

    def test_validate_all_types(self) -> None:
        """Validate each type check branch."""
        skill = EchoSkill()
        # number type
        errors = skill.validate_params("count", {"text": "hi", "uppercase": "not_bool"})
        assert any("must be a boolean" in e for e in errors)

    @pytest.mark.asyncio
    async def test_execute_stream_default(self) -> None:
        skill = EchoSkill()
        await skill.initialize({})
        chunks: list[Any] = []
        async for chunk in skill.execute_stream("echo", {"text": "hello"}):
            chunks.append(chunk)
        assert chunks == ["hello"]

    @pytest.mark.asyncio
    async def test_execute_stream_override(self) -> None:
        skill = StreamSkill()
        await skill.initialize({})
        chunks: list[Any] = []
        async for chunk in skill.execute_stream("words", {"sentence": "a b c"}):
            chunks.append(chunk)
        assert chunks == ["a", "b", "c"]


class TestExceptions:
    def test_hierarchy(self) -> None:
        assert issubclass(SkillInitializationError, SkillError)
        assert issubclass(SkillExecutionError, SkillError)
        assert issubclass(SkillNotFoundError, SkillError)
        assert issubclass(CapabilityNotFoundError, SkillError)


# ---------------------------------------------------------------------------
# Tests: registry.py
# ---------------------------------------------------------------------------


class TestSkillRegistry:
    def setup_method(self) -> None:
        self.registry = SkillRegistry()

    def test_register_and_get(self) -> None:
        skill = EchoSkill()
        self.registry.register(skill)
        assert self.registry.get_skill("echo") is skill

    def test_register_duplicate_raises(self) -> None:
        self.registry.register(EchoSkill())
        with pytest.raises(SkillError, match="already registered"):
            self.registry.register(EchoSkill())

    def test_unregister(self) -> None:
        self.registry.register(EchoSkill())
        self.registry.unregister("echo")
        assert self.registry.get_skill("echo") is None

    def test_unregister_not_found(self) -> None:
        with pytest.raises(SkillNotFoundError):
            self.registry.unregister("missing")

    def test_list_skills(self) -> None:
        self.registry.register(EchoSkill())
        self.registry.register(StreamSkill())
        assert len(self.registry.list_skills()) == 2

    def test_list_capabilities_all(self) -> None:
        self.registry.register(EchoSkill())
        caps = self.registry.list_capabilities()
        assert len(caps) == 2

    def test_list_capabilities_by_skill(self) -> None:
        self.registry.register(EchoSkill())
        self.registry.register(StreamSkill())
        caps = self.registry.list_capabilities("echo")
        assert all(c["skill"] == "echo" for c in caps)

    def test_discover(self) -> None:
        self.registry.register(EchoSkill())
        results = self.registry.discover("echo")
        assert len(results) >= 1
        assert results[0]["skill"] == "echo"

    def test_discover_by_tag(self) -> None:
        self.registry.register(EchoSkill())
        results = self.registry.discover("test")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_initialize_and_execute(self) -> None:
        self.registry.register(EchoSkill())
        await self.registry.initialize_skill("echo", {})
        assert self.registry.is_initialized("echo")
        result = await self.registry.execute("echo.echo", {"text": "hello"})
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_initialize_not_found(self) -> None:
        with pytest.raises(SkillNotFoundError):
            await self.registry.initialize_skill("missing", {})

    @pytest.mark.asyncio
    async def test_initialize_all(self) -> None:
        self.registry.register(EchoSkill())
        self.registry.register(StreamSkill())
        await self.registry.initialize_all()
        assert self.registry.is_initialized("echo")
        assert self.registry.is_initialized("streamer")

    @pytest.mark.asyncio
    async def test_shutdown_skill(self) -> None:
        self.registry.register(EchoSkill())
        await self.registry.initialize_skill("echo", {})
        await self.registry.shutdown_skill("echo")
        assert not self.registry.is_initialized("echo")

    @pytest.mark.asyncio
    async def test_shutdown_all(self) -> None:
        self.registry.register(EchoSkill())
        await self.registry.initialize_all()
        await self.registry.shutdown_all()
        assert not self.registry.is_initialized("echo")

    @pytest.mark.asyncio
    async def test_execute_invalid_path(self) -> None:
        with pytest.raises(ValueError, match="Invalid capability path"):
            await self.registry.execute("no_dot", {})

    @pytest.mark.asyncio
    async def test_execute_not_initialized(self) -> None:
        self.registry.register(EchoSkill())
        with pytest.raises(SkillError, match="not initialized"):
            await self.registry.execute("echo.echo", {"text": "hi"})

    @pytest.mark.asyncio
    async def test_execute_validation_error(self) -> None:
        self.registry.register(EchoSkill())
        await self.registry.initialize_skill("echo", {})
        with pytest.raises(SkillError, match="validation failed"):
            await self.registry.execute("echo.echo", {})

    def test_get_skill_health_not_initialized(self) -> None:
        self.registry.register(EchoSkill())
        health = self.registry.get_skill_health("echo")
        assert health is not None
        assert not health.healthy

    @pytest.mark.asyncio
    async def test_check_skill_health(self) -> None:
        self.registry.register(EchoSkill())
        await self.registry.initialize_skill("echo", {})
        health = await self.registry.check_skill_health("echo")
        assert health.healthy

    @pytest.mark.asyncio
    async def test_check_skill_health_not_found(self) -> None:
        health = await self.registry.check_skill_health("missing")
        assert not health.healthy

    def test_get_stats(self) -> None:
        self.registry.register(EchoSkill())
        stats = self.registry.get_stats()
        assert stats["total_skills"] == 1
        assert stats["total_capabilities"] == 2


class TestGlobalRegistry:
    def setup_method(self) -> None:
        reset_global_registry()

    def teardown_method(self) -> None:
        reset_global_registry()

    def test_singleton(self) -> None:
        r1 = get_global_registry()
        r2 = get_global_registry()
        assert r1 is r2

    def test_reset(self) -> None:
        r1 = get_global_registry()
        reset_global_registry()
        r2 = get_global_registry()
        assert r1 is not r2


# ---------------------------------------------------------------------------
# Tests: loader.py
# ---------------------------------------------------------------------------


class TestSkillLoader:
    def test_load_from_nonexistent_module(self) -> None:
        loader = SkillLoader()
        skills = loader.load_from_module("nonexistent.module.xyz")
        assert skills == []

    def test_load_from_file(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "my_skill.py"
        skill_file.write_text(
            '''\
from obscura.skills.base import (
    Skill, SkillCapability, CapabilityParameter, CapabilityReturn,
    SkillHealth, SkillMetadata,
)
from typing import Any

class TestLoadedSkill(Skill):
    name = "test_loaded"
    version = "0.1.0"
    description = "Dynamically loaded skill"
    metadata = SkillMetadata(author="test")
    capabilities = [
        SkillCapability(
            name="greet",
            description="Greet someone",
            parameters=[CapabilityParameter("name", "string", "Name")],
            returns=CapabilityReturn("string", "Greeting"),
        ),
    ]

    async def initialize(self, config: dict) -> None:
        pass

    async def execute(self, capability: str, params: dict) -> Any:
        return f"Hello {params['name']}"

    async def health_check(self) -> SkillHealth:
        return SkillHealth(healthy=True, message="OK")

    async def shutdown(self) -> None:
        pass
'''
        )

        loader = SkillLoader()
        skills = loader.load_from_file(skill_file)
        assert len(skills) == 1
        assert skills[0].name == "test_loaded"

    def test_load_from_file_not_found(self) -> None:
        loader = SkillLoader()
        with pytest.raises(FileNotFoundError):
            loader.load_from_file("/nonexistent/skill.py")

    def test_load_from_file_not_python(self, tmp_path: Path) -> None:
        txt = tmp_path / "not_python.txt"
        txt.write_text("not python")
        loader = SkillLoader()
        with pytest.raises(ValueError, match="Python file"):
            loader.load_from_file(txt)

    def test_load_from_directory(self, tmp_path: Path) -> None:
        # Create two skill files, one should be skipped (_private)
        (tmp_path / "_private.py").write_text("# ignored")
        skill_file = tmp_path / "greet.py"
        skill_file.write_text(
            '''\
from obscura.skills.base import (
    Skill, SkillCapability, CapabilityParameter, CapabilityReturn,
    SkillHealth, SkillMetadata,
)
from typing import Any

class GreetSkill(Skill):
    name = "greet"
    version = "1.0.0"
    description = "Greets"
    metadata = SkillMetadata(author="test")
    capabilities = [
        SkillCapability(
            name="hello",
            description="Say hello",
            parameters=[],
            returns=CapabilityReturn("string", "Greeting"),
        ),
    ]

    async def initialize(self, config: dict) -> None:
        pass

    async def execute(self, capability: str, params: dict) -> Any:
        return "Hello!"

    async def health_check(self) -> SkillHealth:
        return SkillHealth(healthy=True, message="OK")

    async def shutdown(self) -> None:
        pass
'''
        )

        loader = SkillLoader()
        skills = loader.load_from_directory(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "greet"

    def test_load_from_directory_not_found(self) -> None:
        loader = SkillLoader()
        with pytest.raises(FileNotFoundError):
            loader.load_from_directory("/nonexistent/dir")


# ---------------------------------------------------------------------------
# Tests: docs_loader.py
# ---------------------------------------------------------------------------


class TestDocsLoader:
    def test_load_markdown_documents(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "setup.md").write_text("# Setup\nDo things.")
        (skills_dir / "empty.md").write_text("")  # should be skipped

        sub = skills_dir / "advanced"
        sub.mkdir()
        (sub / "deploy.md").write_text("# Deploy\nDeploy stuff.")

        docs = load_markdown_skill_documents(skills_dir)
        assert len(docs) == 2
        names = {d.name for d in docs}
        assert "setup" in names
        assert "advanced/deploy" in names

    def test_load_from_missing_dir(self, tmp_path: Path) -> None:
        docs = load_markdown_skill_documents(tmp_path / "missing")
        assert docs == []

    def test_load_texts(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "a.md").write_text("Alpha")
        (skills_dir / "b.md").write_text("Beta")
        texts = load_markdown_skill_texts(skills_dir)
        assert set(texts) == {"Alpha", "Beta"}

    def test_markdown_document_frozen(self) -> None:
        doc = MarkdownSkillDocument(name="test", path=Path("/x"), content="y")
        with pytest.raises(AttributeError):
            doc.name = "changed"  # type: ignore[misc]

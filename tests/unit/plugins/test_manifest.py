"""Comprehensive tests for obscura.plugins.manifest parser."""

from __future__ import annotations

from typing import Any

import json
from pathlib import Path

import pytest
import tomli_w

from obscura.plugins.manifest import (
    ManifestError,
    parse_bootstrap,
    parse_capabilities,
    parse_config_requirements,
    parse_healthcheck,
    parse_instructions,
    parse_policy_hints,
    parse_tools,
    parse_workflows,
    parse_manifest,
    parse_manifest_file,
)
from obscura.plugins.models import (
    BootstrapDep,
    BootstrapSpec,
    HealthcheckSpec,
    PluginSpec,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

MINIMAL_DATA = {"id": "test-plugin", "version": "1.0.0"}


def _full_manifest() -> dict[str, Any]:
    return {
        "id": "full-plugin",
        "name": "Full Plugin",
        "version": "2.1.0",
        "source_type": "git",
        "runtime_type": "sdk",
        "trust_level": "verified",
        "author": "tester",
        "description": "A fully-specified plugin.",
        "config": {
            "API_KEY": {"type": "secret", "required": True, "description": "key"},
        },
        "capabilities": [
            {
                "id": "net.fetch",
                "version": "1.0.0",
                "description": "HTTP fetching",
                "tools": ["http_get"],
            },
        ],
        "tools": [
            {
                "name": "http_get",
                "description": "GET request",
                "parameters": {"url": {"type": "string"}},
                "handler": "full_plugin.tools:http_get",
                "capability": "net.fetch",
                "side_effects": "read",
            },
        ],
        "workflows": [
            {
                "id": "fetch-flow",
                "version": "1.0.0",
                "name": "Fetch Flow",
                "description": "Fetches data",
                "steps": [{"tool": "http_get"}],
                "required_capabilities": ["net.fetch"],
            },
        ],
        "instructions": [
            {
                "id": "greet",
                "version": "1.0.0",
                "scope": "agent",
                "content": "Say hello.",
                "priority": 10,
            },
        ],
        "policy_hints": [
            {
                "capability_id": "net.fetch",
                "recommended_action": "allow",
                "reason": "safe read-only",
            },
        ],
        "healthcheck": {
            "type": "http",
            "target": "http://localhost:8080/health",
            "interval_seconds": 60,
        },
        "bootstrap": {
            "deps": [{"type": "pip", "package": "httpx", "version": ">=0.24"}],
            "post_install": "echo done",
            "check_command": "httpx --version",
        },
    }


# ===================================================================
# 1. parse_manifest — top-level
# ===================================================================


class TestParseManifest:
    def test_minimal(self) -> None:
        spec = parse_manifest(MINIMAL_DATA)
        assert isinstance(spec, PluginSpec)
        assert spec.id == "test-plugin"
        assert spec.version == "1.0.0"
        assert spec.name == "test-plugin"  # defaults to id

    def test_defaults(self) -> None:
        spec = parse_manifest(MINIMAL_DATA)
        assert spec.source_type == "local"
        assert spec.runtime_type == "native"
        assert spec.trust_level == "community"
        assert spec.author == ""
        assert spec.description == ""
        assert spec.config_requirements == ()
        assert spec.capabilities == ()
        assert spec.tools == ()
        assert spec.workflows == ()
        assert spec.instructions == ()
        assert spec.policy_hints == ()
        assert spec.bootstrap is None
        assert spec.healthcheck is None

    def test_full_manifest(self) -> None:
        spec = parse_manifest(_full_manifest())
        assert spec.id == "full-plugin"
        assert spec.name == "Full Plugin"
        assert spec.version == "2.1.0"
        assert spec.source_type == "git"
        assert spec.runtime_type == "sdk"
        assert spec.trust_level == "verified"
        assert spec.author == "tester"
        assert len(spec.config_requirements) == 1
        assert len(spec.capabilities) == 1
        assert len(spec.tools) == 1
        assert len(spec.workflows) == 1
        assert len(spec.instructions) == 1
        assert len(spec.policy_hints) == 1
        assert spec.healthcheck is not None
        assert spec.bootstrap is not None

    def test_missing_id_raises(self) -> None:
        with pytest.raises(ManifestError, match="Missing required field.*'id'"):
            parse_manifest({"version": "1.0.0"})

    def test_missing_version_raises(self) -> None:
        with pytest.raises(ManifestError, match="Missing required field.*'version'"):
            parse_manifest({"id": "test-plugin"})

    def test_invalid_id_raises(self) -> None:
        with pytest.raises(ManifestError):
            parse_manifest({"id": "INVALID ID!", "version": "1.0.0"})

    def test_invalid_version_raises(self) -> None:
        with pytest.raises(ManifestError):
            parse_manifest({"id": "test-plugin", "version": "not-semver"})

    def test_source_path_in_error(self) -> None:
        p = Path("/fake/plugin.toml")
        with pytest.raises(ManifestError) as exc_info:
            parse_manifest({"version": "1.0.0"}, source_path=p)
        assert exc_info.value.path == p
        assert "/fake/plugin.toml" in str(exc_info.value)


# ===================================================================
# 2. Config parsing
# ===================================================================


class TestParseConfigRequirements:
    def test_none_returns_empty(self) -> None:
        assert parse_config_requirements(None) == ()

    def test_dict_form(self) -> None:
        raw = {
            "API_KEY": {"type": "secret", "required": True, "description": "api key"},
            "TIMEOUT": {"type": "int", "required": False, "default": "30"},
        }
        result = parse_config_requirements(raw)
        assert len(result) == 2
        by_key = {r.key: r for r in result}
        assert by_key["API_KEY"].type == "secret"
        assert by_key["API_KEY"].required is True
        assert by_key["TIMEOUT"].required is False
        assert by_key["TIMEOUT"].default == "30"

    def test_dict_form_non_dict_spec(self) -> None:
        # When value is not a dict (e.g. bare string), fallback to defaults
        result = parse_config_requirements({"SIMPLE_KEY": "just a note"})
        assert len(result) == 1
        assert result[0].key == "SIMPLE_KEY"
        assert result[0].type == "string"

    def test_list_form(self) -> None:
        raw = [
            {"key": "DB_HOST", "type": "string", "required": True},
            {"name": "DB_PORT", "type": "int", "required": False},
        ]
        result = parse_config_requirements(raw)
        assert len(result) == 2
        assert result[0].key == "DB_HOST"
        assert result[1].key == "DB_PORT"
        assert result[1].type == "int"


# ===================================================================
# 3. Capability parsing
# ===================================================================


class TestParseCapabilities:
    def test_none_returns_empty(self) -> None:
        assert parse_capabilities(None) == ()

    def test_empty_list(self) -> None:
        assert parse_capabilities([]) == ()

    def test_basic(self) -> None:
        raw = [
            {
                "id": "repo.read",
                "version": "1.0.0",
                "description": "Read repos",
                "tools": ["git_log", "git_diff"],
            },
        ]
        result = parse_capabilities(raw)
        assert len(result) == 1
        assert result[0].id == "repo.read"
        assert result[0].tools == ("git_log", "git_diff")

    def test_tools_as_string(self) -> None:
        raw = [{"id": "repo.read", "version": "1.0.0", "tools": "git_log"}]
        result = parse_capabilities(raw)
        assert result[0].tools == ("git_log",)

    def test_defaults(self) -> None:
        raw = [{"id": "repo.read", "version": "1.0.0"}]
        result = parse_capabilities(raw)
        assert result[0].requires_approval is False
        assert result[0].default_grant is True
        assert result[0].description == ""


# ===================================================================
# 4. Tool parsing
# ===================================================================


class TestParseTools:
    def test_none_returns_empty(self) -> None:
        assert parse_tools(None) == ()

    def test_empty_list(self) -> None:
        assert parse_tools([]) == ()

    def test_full_tool(self) -> None:
        raw = [
            {
                "name": "search",
                "description": "Search things",
                "parameters": {"q": {"type": "string"}},
                "handler": "mod:search",
                "capability": "data.search",
                "side_effects": "read",
                "timeout_seconds": 30,
                "retries": 2,
            },
        ]
        result = parse_tools(raw)
        assert len(result) == 1
        t = result[0]
        assert t.name == "search"
        assert t.handler_ref == "mod:search"
        assert t.capability == "data.search"
        assert t.side_effects == "read"
        assert t.timeout_seconds == 30.0
        assert t.retries == 2

    def test_handler_ref_alias(self) -> None:
        raw = [{"name": "tool1", "handler_ref": "mod:func"}]
        result = parse_tools(raw)
        assert result[0].handler_ref == "mod:func"

    def test_defaults(self) -> None:
        raw = [{"name": "minimal"}]
        result = parse_tools(raw)
        t = result[0]
        assert t.description == ""
        assert t.parameters == {}
        assert t.handler_ref == ""
        assert t.side_effects == "none"
        assert t.timeout_seconds == 60.0
        assert t.retries == 0


# ===================================================================
# 5. Bootstrap parsing
# ===================================================================


class TestParseBootstrap:
    def test_none_returns_none(self) -> None:
        assert parse_bootstrap(None) is None

    def test_full_spec(self) -> None:
        raw = {
            "deps": [{"type": "pip", "package": "httpx"}],
            "post_install": "echo done",
            "check_command": "httpx --version",
        }
        result = parse_bootstrap(raw)
        assert result is not None
        assert isinstance(result, BootstrapSpec)
        assert len(result.deps) == 1
        assert result.deps[0].type == "pip"
        assert result.deps[0].package == "httpx"
        assert result.post_install == "echo done"
        assert result.check_command == "httpx --version"

    def test_shorthand_strings(self) -> None:
        raw = {"deps": ["pip:httpx", "binary:gws"]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert len(result.deps) == 2
        assert result.deps[0] == BootstrapDep(type="pip", package="httpx")
        assert result.deps[1] == BootstrapDep(type="binary", package="gws")

    def test_shorthand_no_colon_defaults_pip(self) -> None:
        raw = {"deps": ["requests"]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.deps[0].type == "pip"
        assert result.deps[0].package == "requests"

    def test_list_only_form(self) -> None:
        raw = [{"type": "pip", "package": "httpx"}]
        result = parse_bootstrap(raw)
        assert result is not None
        assert isinstance(result, BootstrapSpec)
        assert len(result.deps) == 1
        assert result.deps[0].package == "httpx"

    def test_dep_optional_flag(self) -> None:
        raw = {"deps": [{"type": "pip", "package": "opt", "optional": True}]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.deps[0].optional is True

    def test_empty_dict_returns_none(self) -> None:
        assert parse_bootstrap({}) is None

    def test_empty_list_returns_none(self) -> None:
        assert parse_bootstrap([]) is None

    def test_all_dep_types_shorthand(self) -> None:
        """All 8 dep types parse correctly in shorthand form."""
        raw = {
            "deps": [
                "pip:requests",
                "uv:ruff",
                "npx:prettier",
                "npm:eslint",
                "cargo:ripgrep",
                "binary:git",
                "brew:jq",
                "pipx:black",
            ],
        }
        result = parse_bootstrap(raw)
        assert result is not None
        assert len(result.deps) == 8
        by_type = {d.type: d.package for d in result.deps}
        assert by_type == {
            "pip": "requests",
            "uv": "ruff",
            "npx": "prettier",
            "npm": "eslint",
            "cargo": "ripgrep",
            "binary": "git",
            "brew": "jq",
            "pipx": "black",
        }

    def test_shorthand_with_version(self) -> None:
        """Shorthand 'pip:requests>=2.0' keeps version in package string."""
        raw = {"deps": ["pip:requests>=2.0"]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.deps[0].type == "pip"
        assert result.deps[0].package == "requests>=2.0"

    def test_shorthand_with_extras(self) -> None:
        """Shorthand 'pip:my-pkg[extra]' keeps extras in package string."""
        raw = {"deps": ["pip:my-pkg[extra]"]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.deps[0].package == "my-pkg[extra]"

    def test_dict_with_version(self) -> None:
        raw = {"deps": [{"type": "pip", "package": "click", "version": ">=8.0"}]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.deps[0].version == ">=8.0"

    def test_mixed_string_and_dict_deps(self) -> None:
        raw = {"deps": ["pip:requests", {"type": "binary", "package": "git"}]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert len(result.deps) == 2
        assert result.deps[0] == BootstrapDep(type="pip", package="requests")
        assert result.deps[1].type == "binary"
        assert result.deps[1].package == "git"

    def test_post_install_preserved(self) -> None:
        raw = {"deps": [{"type": "pip", "package": "x"}], "post_install": "echo ok"}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.post_install == "echo ok"

    def test_check_command_preserved(self) -> None:
        raw = {
            "deps": [{"type": "pip", "package": "x"}],
            "check_command": "foo --version",
        }
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.check_command == "foo --version"

    def test_dict_optional_true(self) -> None:
        raw = {"deps": [{"type": "npm", "package": "pkg", "optional": True}]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.deps[0].optional is True

    def test_dict_optional_defaults_false(self) -> None:
        raw = {"deps": [{"type": "npm", "package": "pkg"}]}
        result = parse_bootstrap(raw)
        assert result is not None
        assert result.deps[0].optional is False


# ===================================================================
# 6. Workflow, instruction, policy_hint parsing
# ===================================================================


class TestParseWorkflows:
    def test_none_returns_empty(self) -> None:
        assert parse_workflows(None) == ()

    def test_empty_list(self) -> None:
        assert parse_workflows([]) == ()

    def test_basic_workflow(self) -> None:
        raw = [
            {
                "id": "flow1",
                "version": "1.0.0",
                "name": "Flow One",
                "description": "A flow",
                "steps": [{"tool": "t1"}],
                "required_capabilities": ["cap.one"],
            },
        ]
        result = parse_workflows(raw)
        assert len(result) == 1
        assert result[0].id == "flow1"
        assert result[0].steps == ({"tool": "t1"},)
        assert result[0].required_capabilities == ("cap.one",)

    def test_capabilities_as_string(self) -> None:
        raw = [{"id": "flow2", "version": "1.0.0", "required_capabilities": "cap.one"}]
        result = parse_workflows(raw)
        assert result[0].required_capabilities == ("cap.one",)

    def test_name_defaults_to_id(self) -> None:
        raw = [{"id": "flow3", "version": "1.0.0"}]
        result = parse_workflows(raw)
        assert result[0].name == "flow3"


class TestParseInstructions:
    def test_none_returns_empty(self) -> None:
        assert parse_instructions(None) == ()

    def test_empty_list(self) -> None:
        assert parse_instructions([]) == ()

    def test_basic(self) -> None:
        raw = [
            {"id": "instr1", "scope": "agent", "content": "Do this.", "priority": 90},
        ]
        result = parse_instructions(raw)
        assert len(result) == 1
        assert result[0].id == "instr1"
        assert result[0].scope == "agent"
        assert result[0].content == "Do this."
        assert result[0].priority == 90

    def test_defaults(self) -> None:
        raw = [{"id": "instr2", "content": "Some content."}]
        result = parse_instructions(raw)
        assert result[0].scope == "agent"
        assert result[0].priority == 50


class TestParsePolicyHints:
    def test_none_returns_empty(self) -> None:
        assert parse_policy_hints(None) == ()

    def test_empty_list(self) -> None:
        assert parse_policy_hints([]) == ()

    def test_basic(self) -> None:
        raw = [
            {
                "capability_id": "net.fetch",
                "recommended_action": "deny",
                "reason": "not safe",
            },
        ]
        result = parse_policy_hints(raw)
        assert len(result) == 1
        assert result[0].capability_id == "net.fetch"
        assert result[0].recommended_action == "deny"
        assert result[0].reason == "not safe"

    def test_defaults(self) -> None:
        raw = [{"capability_id": "net.fetch"}]
        result = parse_policy_hints(raw)
        assert result[0].recommended_action == "allow"
        assert result[0].reason == ""


# ===================================================================
# 7. Healthcheck parsing
# ===================================================================


class TestParseHealthcheck:
    def test_none_returns_none(self) -> None:
        assert parse_healthcheck(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert parse_healthcheck({}) is None

    def test_valid(self) -> None:
        raw = {
            "type": "http",
            "target": "http://localhost/health",
            "interval_seconds": 120,
        }
        result = parse_healthcheck(raw)
        assert result is not None
        assert isinstance(result, HealthcheckSpec)
        assert result.type == "http"
        assert result.target == "http://localhost/health"
        assert result.interval_seconds == 120

    def test_defaults(self) -> None:
        raw = {"type": "callable", "target": "mod:check"}
        result = parse_healthcheck(raw)
        assert result is not None
        assert result.interval_seconds == 300


# ===================================================================
# 8. parse_manifest_file
# ===================================================================


class TestParseManifestFile:
    def test_valid_toml(self, tmp_path: Path) -> None:
        f = tmp_path / "plugin.toml"
        f.write_bytes(tomli_w.dumps(MINIMAL_DATA).encode())
        spec = parse_manifest_file(f)
        assert spec.id == "test-plugin"
        assert spec.version == "1.0.0"

    def test_valid_full_toml(self, tmp_path: Path) -> None:
        f = tmp_path / "plugin.toml"
        f.write_bytes(tomli_w.dumps(_full_manifest()).encode())
        spec = parse_manifest_file(f)
        assert spec.id == "full-plugin"
        assert len(spec.tools) == 1

    def test_nonexistent_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.toml"
        with pytest.raises(ManifestError, match="not found"):
            parse_manifest_file(missing)

    def test_invalid_toml_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.toml"
        f.write_text("{{{{not valid toml")
        with pytest.raises(ManifestError, match="Failed to parse"):
            parse_manifest_file(f)

    def test_non_mapping_raises(self, tmp_path: Path) -> None:
        """TOML top-level is always a table (mapping), so we test that a
        file missing required fields still goes through the mapping check.
        We write a valid TOML file whose content is a mapping but that
        lacks required manifest keys, verifying the parse path works.
        For the actual 'must be a mapping' branch we use a JSON file
        containing a bare list, since TOML cannot represent a non-mapping
        top-level.
        """
        f = tmp_path / "list.json"
        f.write_text(json.dumps(["not", "a", "mapping"]))
        with pytest.raises(ManifestError, match="must be a mapping"):
            parse_manifest_file(f)

    def test_json_file(self, tmp_path: Path) -> None:
        """JSON fallback works when yaml module is available too, but we
        test via a .json file parsed through the yaml loader (yaml handles JSON).
        """
        f = tmp_path / "plugin.json"
        f.write_text(json.dumps(MINIMAL_DATA))
        spec = parse_manifest_file(f)
        assert spec.id == "test-plugin"


# ===================================================================
# 9. Real builtin manifests
# ===================================================================


class TestBuiltinManifests:
    def test_all_builtins_parse(self) -> None:
        from obscura.plugins.builtins import list_builtin_manifests

        manifests = list_builtin_manifests()
        assert len(manifests) >= 13, f"Expected ≥13 builtins, got {len(manifests)}"
        for manifest_path in manifests:
            spec = parse_manifest_file(manifest_path)
            assert spec.id, f"Manifest {manifest_path.name} has no id"
            assert spec.version, f"Manifest {manifest_path.name} has no version"
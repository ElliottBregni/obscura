"""Comprehensive tests for obscura.plugins.validator."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from obscura.plugins.models import (
    CapabilitySpec,
    ConfigRequirement,
    PluginSpec,
    PolicyHintSpec,
    ToolContribution,
    WorkflowSpec,
)
from obscura.plugins.validator import ValidationError, is_valid, validate_plugin_spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(**overrides) -> PluginSpec:
    """Build a minimal valid PluginSpec, merging *overrides*."""
    defaults = dict(
        id="test-plugin",
        name="Test Plugin",
        version="1.0.0",
        source_type="local",
        runtime_type="native",
    )
    defaults.update(overrides)
    return PluginSpec(**defaults)


def _make_cap(id: str = "test.read", **kw) -> CapabilitySpec:
    return CapabilitySpec(id=id, version="1.0.0", description="cap", **kw)


def _make_tool(name: str = "do-stuff", **kw) -> ToolContribution:
    return ToolContribution(name=name, description="tool desc", **kw)


# ---------------------------------------------------------------------------
# 1. Completely valid spec → empty error list
# ---------------------------------------------------------------------------

class TestValidSpec:
    def test_minimal_valid_spec(self):
        spec = _make_spec()
        errors = validate_plugin_spec(spec)
        assert errors == []

    def test_full_valid_spec(self):
        cap = _make_cap("test.read", tools=("search",))
        tool = _make_tool("search", capability="test.read", handler_ref="my.mod:func")
        wf = WorkflowSpec(
            id="wf1", version="1.0.0", name="WF", description="d",
            required_capabilities=("test.read",),
        )
        cfg = ConfigRequirement(key="API_KEY", type="string")
        hint = PolicyHintSpec(capability_id="test.read", recommended_action="allow")
        spec = _make_spec(
            capabilities=(cap,),
            tools=(tool,),
            workflows=(wf,),
            config_requirements=(cfg,),
            policy_hints=(hint,),
            install_hook="my.mod:install",
            bootstrap_hook="my.mod:bootstrap",
        )
        errors = validate_plugin_spec(spec)
        assert errors == []


# ---------------------------------------------------------------------------
# 2. Duplicate tool names
# ---------------------------------------------------------------------------

class TestDuplicateToolNames:
    def test_duplicate_tool_name_produces_error(self):
        tools = (_make_tool("dup"), _make_tool("dup"))
        spec = _make_spec(tools=tools)
        errors = validate_plugin_spec(spec)
        assert any("Duplicate tool name" in e.message for e in errors)
        assert all(e.severity == "error" for e in errors if "Duplicate" in e.message)

    def test_distinct_tool_names_ok(self):
        tools = (_make_tool("a"), _make_tool("b"))
        spec = _make_spec(tools=tools)
        assert validate_plugin_spec(spec) == []


# ---------------------------------------------------------------------------
# 3. Tool references undeclared capability
# ---------------------------------------------------------------------------

class TestToolCapabilityRef:
    def test_undeclared_capability_is_error(self):
        tool = _make_tool("t", capability="nonexistent.cap")
        spec = _make_spec(tools=(tool,))
        errors = validate_plugin_spec(spec)
        assert any("undeclared capability" in e.message.lower() for e in errors)
        assert any(e.severity == "error" for e in errors)

    def test_declared_capability_ok(self):
        cap = _make_cap("test.read")
        tool = _make_tool("t", capability="test.read")
        spec = _make_spec(capabilities=(cap,), tools=(tool,))
        errs = [e for e in validate_plugin_spec(spec) if e.severity == "error"]
        assert errs == []


# ---------------------------------------------------------------------------
# 4. Invalid handler_ref format
# ---------------------------------------------------------------------------

class TestHandlerRef:
    @pytest.mark.parametrize("ref", [
        "my.module:func",
        "my.module.path",
        "a:b",
        "_private.mod:_func",
    ])
    def test_valid_handler_refs(self, ref):
        tool = _make_tool("t", handler_ref=ref)
        spec = _make_spec(tools=(tool,))
        errs = [e for e in validate_plugin_spec(spec) if "handler" in e.field]
        assert errs == []

    @pytest.mark.parametrize("ref", [
        "123bad",
        ":func",
        "mod:",
        "has space:func",
        "mod:func:extra",
    ])
    def test_invalid_handler_refs(self, ref):
        tool = _make_tool("t", handler_ref=ref)
        spec = _make_spec(tools=(tool,))
        errs = [e for e in validate_plugin_spec(spec) if "handler" in e.field]
        assert len(errs) >= 1
        assert errs[0].severity == "error"

    def test_empty_handler_ref_is_allowed(self):
        tool = _make_tool("t", handler_ref="")
        spec = _make_spec(tools=(tool,))
        errs = [e for e in validate_plugin_spec(spec) if "handler" in e.field]
        assert errs == []


# ---------------------------------------------------------------------------
# 5. Config type validation
# ---------------------------------------------------------------------------

class TestConfigTypes:
    @pytest.mark.parametrize("ctype", ["string", "int", "float", "bool", "secret", "list"])
    def test_valid_config_types(self, ctype):
        cfg = ConfigRequirement(key="K", type=ctype)
        spec = _make_spec(config_requirements=(cfg,))
        errs = [e for e in validate_plugin_spec(spec) if "config" in e.field.lower()]
        assert errs == []

    @pytest.mark.parametrize("ctype", ["path", "url", "object", ""])
    def test_invalid_config_type_produces_warning(self, ctype):
        cfg = ConfigRequirement(key="K", type=ctype)
        spec = _make_spec(config_requirements=(cfg,))
        errs = [e for e in validate_plugin_spec(spec) if "config" in e.field.lower()]
        assert len(errs) >= 1
        assert errs[0].severity == "warning"


# ---------------------------------------------------------------------------
# 6. Workflow references undeclared capability → warning
# ---------------------------------------------------------------------------

class TestWorkflowCapabilities:
    def test_undeclared_capability_is_warning(self):
        wf = WorkflowSpec(
            id="w1", version="1.0.0", name="W", description="d",
            required_capabilities=("nope.nope",),
        )
        spec = _make_spec(workflows=(wf,))
        errs = [e for e in validate_plugin_spec(spec) if "workflow" in e.field.lower()]
        assert len(errs) >= 1
        assert errs[0].severity == "warning"

    def test_declared_capability_ok(self):
        cap = _make_cap("test.read")
        wf = WorkflowSpec(
            id="w1", version="1.0.0", name="W", description="d",
            required_capabilities=("test.read",),
        )
        spec = _make_spec(capabilities=(cap,), workflows=(wf,))
        errs = [e for e in validate_plugin_spec(spec) if "workflow" in e.field.lower()]
        assert errs == []


# ---------------------------------------------------------------------------
# 7. ValidationError severity levels
# ---------------------------------------------------------------------------

class TestSeverityLevels:
    def test_error_severity(self):
        err = ValidationError(field="f", message="m", severity="error")
        assert err.severity == "error"
        assert "[error]" in str(err)

    def test_warning_severity(self):
        err = ValidationError(field="f", message="m", severity="warning")
        assert err.severity == "warning"
        assert "[warning]" in str(err)

    def test_default_severity_is_error(self):
        err = ValidationError(field="f", message="m")
        assert err.severity == "error"

    def test_strict_promotes_warnings_to_errors(self):
        wf = WorkflowSpec(
            id="w1", version="1.0.0", name="W", description="d",
            required_capabilities=("nope.nope",),
        )
        spec = _make_spec(workflows=(wf,))
        normal = validate_plugin_spec(spec)
        assert any(e.severity == "warning" for e in normal)

        strict = validate_plugin_spec(spec, strict=True)
        assert all(e.severity == "error" for e in strict)


# ---------------------------------------------------------------------------
# 8. is_valid() convenience function
# ---------------------------------------------------------------------------

class TestIsValid:
    def test_valid_spec_returns_true(self):
        assert is_valid(_make_spec()) is True

    def test_spec_with_errors_returns_false(self):
        tools = (_make_tool("dup"), _make_tool("dup"))
        spec = _make_spec(tools=tools)
        assert is_valid(spec) is False

    def test_warnings_only_returns_true(self):
        wf = WorkflowSpec(
            id="w1", version="1.0.0", name="W", description="d",
            required_capabilities=("nope.nope",),
        )
        spec = _make_spec(workflows=(wf,))
        assert is_valid(spec) is True

    def test_warnings_strict_returns_false(self):
        wf = WorkflowSpec(
            id="w1", version="1.0.0", name="W", description="d",
            required_capabilities=("nope.nope",),
        )
        spec = _make_spec(workflows=(wf,))
        assert is_valid(spec, strict=True) is False


# ---------------------------------------------------------------------------
# 9. All 13 builtin manifests pass validation
# ---------------------------------------------------------------------------

_BUILTINS_DIR = Path(__file__).resolve().parents[3] / "obscura" / "plugins" / "builtins"


def _builtin_yamls():
    return sorted(_BUILTINS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("manifest_path", _builtin_yamls(), ids=lambda p: p.stem)
def test_builtin_manifest_validates(manifest_path):
    from obscura.plugins.manifest import parse_manifest_file
    spec = parse_manifest_file(manifest_path)
    errors = validate_plugin_spec(spec)
    hard_errors = [e for e in errors if e.severity == "error"]
    assert hard_errors == [], (
        f"{manifest_path.name} has validation errors:\n"
        + "\n".join(str(e) for e in hard_errors)
    )


# ---------------------------------------------------------------------------
# 10. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_tools_list(self):
        spec = _make_spec(tools=())
        assert validate_plugin_spec(spec) == []

    def test_empty_capabilities(self):
        spec = _make_spec(capabilities=())
        assert validate_plugin_spec(spec) == []

    def test_no_handler_refs(self):
        tool = _make_tool("t", handler_ref="")
        spec = _make_spec(tools=(tool,))
        assert validate_plugin_spec(spec) == []

    def test_tool_with_empty_name(self):
        tool = ToolContribution(name="", description="d")
        spec = _make_spec(tools=(tool,))
        errors = validate_plugin_spec(spec)
        assert any("empty name" in e.message.lower() for e in errors)

    def test_config_with_empty_key(self):
        cfg = ConfigRequirement(key="")
        spec = _make_spec(config_requirements=(cfg,))
        errors = validate_plugin_spec(spec)
        assert any("empty key" in e.message.lower() for e in errors)

    def test_capability_references_undeclared_tool_is_warning(self):
        cap = _make_cap("test.read", tools=("ghost-tool",))
        spec = _make_spec(capabilities=(cap,))
        errors = validate_plugin_spec(spec)
        assert any(e.severity == "warning" and "undeclared tool" in e.message.lower() for e in errors)

    def test_invalid_install_hook(self):
        spec = _make_spec(install_hook="123bad")
        errors = validate_plugin_spec(spec)
        assert any("install_hook" in e.field for e in errors)

    def test_invalid_bootstrap_hook(self):
        spec = _make_spec(bootstrap_hook=":bad")
        errors = validate_plugin_spec(spec)
        assert any("bootstrap_hook" in e.field for e in errors)

    def test_valid_hooks(self):
        spec = _make_spec(install_hook="my.mod:setup", bootstrap_hook="my.mod:boot")
        errs = [e for e in validate_plugin_spec(spec) if "hook" in e.field]
        assert errs == []

    def test_policy_hint_undeclared_capability_is_warning(self):
        cap = _make_cap("test.read")
        hint = PolicyHintSpec(capability_id="test.read", recommended_action="allow")
        hint_bad = PolicyHintSpec(capability_id="other.cap", recommended_action="deny")
        spec = _make_spec(capabilities=(cap,), policy_hints=(hint, hint_bad))
        errors = validate_plugin_spec(spec)
        warnings = [e for e in errors if e.severity == "warning" and "policy_hint" in e.field.lower()]
        assert len(warnings) >= 1

    def test_validation_error_str(self):
        err = ValidationError(field="tools.foo", message="bad", severity="error")
        assert str(err) == "[error] tools.foo: bad"

    def test_validation_error_is_frozen(self):
        err = ValidationError(field="x", message="y")
        with pytest.raises(AttributeError):
            err.field = "z"  # type: ignore[misc]

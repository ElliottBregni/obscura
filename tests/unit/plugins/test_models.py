"""Comprehensive tests for obscura.plugins.models."""

from __future__ import annotations

import dataclasses

import pytest

from obscura.plugins.models import (
    BootstrapDep,
    BootstrapSpec,
    CapabilitySpec,
    ConfigRequirement,
    HealthcheckSpec,
    InstructionSpec,
    PluginSpec,
    PluginStatus,
    PolicyHintSpec,
    RUNTIME_TYPES,
    SOURCE_TYPES,
    TRUST_LEVELS,
    ToolContribution,
    WorkflowSpec,
    validate_capability_id,
    validate_plugin_id,
    validate_semver,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _minimal_plugin(**overrides) -> PluginSpec:
    defaults = dict(
        id="my-plugin",
        name="My Plugin",
        version="1.0.0",
        source_type="local",
        runtime_type="native",
    )
    defaults.update(overrides)
    return PluginSpec(**defaults)


# ── validate_semver ──────────────────────────────────────────────────────────


class TestValidateSemver:
    @pytest.mark.parametrize(
        "v",
        [
            "0.0.0",
            "1.0.0",
            "1.2.3",
            "10.20.30",
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0+build.123",
            "1.0.0-beta+build.456",
        ],
    )
    def test_valid(self, v: str):
        assert validate_semver(v) == v

    @pytest.mark.parametrize(
        "v",
        ["", "1", "1.0", "v1.0.0", "1.0.0.", "01.0.0", "1.0.01", "abc"],
    )
    def test_invalid(self, v: str):
        with pytest.raises(ValueError, match="Invalid semver"):
            validate_semver(v)


# ── validate_capability_id ───────────────────────────────────────────────────


class TestValidateCapabilityId:
    @pytest.mark.parametrize(
        "cid",
        ["repo.read", "shell.exec", "a.b.c", "repo0.read_all"],
    )
    def test_valid(self, cid: str):
        assert validate_capability_id(cid) == cid

    @pytest.mark.parametrize(
        "cid",
        [
            "",
            "read",              # no dot
            ".read",             # starts with dot
            "Repo.read",         # uppercase
            "repo.",             # trailing dot
            "repo..read",        # double dot
            "1repo.read",        # starts with digit
            "repo.1read",        # segment starts with digit
        ],
    )
    def test_invalid(self, cid: str):
        with pytest.raises(ValueError, match="Invalid capability ID"):
            validate_capability_id(cid)


# ── validate_plugin_id ──────────────────────────────────────────────────────


class TestValidatePluginId:
    @pytest.mark.parametrize(
        "pid",
        ["myplugin", "my-plugin", "my_plugin", "a", "a1b2"],
    )
    def test_valid(self, pid: str):
        assert validate_plugin_id(pid) == pid

    @pytest.mark.parametrize(
        "pid",
        ["", "My-Plugin", "1plugin", "-plugin", "my plugin", "my.plugin"],
    )
    def test_invalid(self, pid: str):
        with pytest.raises(ValueError, match="Invalid plugin ID"):
            validate_plugin_id(pid)


# ── BootstrapDep ─────────────────────────────────────────────────────────────


class TestBootstrapDep:
    @pytest.mark.parametrize("dep_type", ["pip", "uv", "npx", "binary", "npm", "cargo"])
    def test_valid_types(self, dep_type: str):
        dep = BootstrapDep(type=dep_type, package="some-pkg")
        assert dep.type == dep_type

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Unknown bootstrap dep type"):
            BootstrapDep(type="conda", package="pkg")

    def test_empty_package(self):
        with pytest.raises(ValueError, match="must not be empty"):
            BootstrapDep(type="pip", package="")

    def test_whitespace_only_package(self):
        with pytest.raises(ValueError, match="must not be empty"):
            BootstrapDep(type="pip", package="   ")

    def test_defaults(self):
        dep = BootstrapDep(type="pip", package="requests")
        assert dep.version == ""
        assert dep.optional is False

    def test_frozen(self):
        dep = BootstrapDep(type="pip", package="requests")
        with pytest.raises(dataclasses.FrozenInstanceError):
            dep.package = "other"  # type: ignore[misc]


# ── BootstrapSpec ────────────────────────────────────────────────────────────


class TestBootstrapSpec:
    def test_defaults(self):
        spec = BootstrapSpec()
        assert spec.deps == ()
        assert spec.post_install == ""
        assert spec.check_command == ""

    def test_with_deps(self):
        dep = BootstrapDep(type="pip", package="requests", version=">=2.0")
        spec = BootstrapSpec(deps=(dep,))
        assert len(spec.deps) == 1

    def test_frozen(self):
        spec = BootstrapSpec()
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.post_install = "echo hi"  # type: ignore[misc]


# ── HealthcheckSpec ──────────────────────────────────────────────────────────


class TestHealthcheckSpec:
    @pytest.mark.parametrize("hc_type", ["callable", "http", "binary"])
    def test_valid_types(self, hc_type: str):
        hc = HealthcheckSpec(type=hc_type, target="check")
        assert hc.type == hc_type

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Unknown healthcheck type"):
            HealthcheckSpec(type="grpc", target="check")

    def test_default_interval(self):
        hc = HealthcheckSpec(type="http", target="http://localhost:8080/health")
        assert hc.interval_seconds == 300

    def test_interval_zero(self):
        with pytest.raises(ValueError, match="interval_seconds must be >= 1"):
            HealthcheckSpec(type="callable", target="fn", interval_seconds=0)

    def test_interval_negative(self):
        with pytest.raises(ValueError, match="interval_seconds must be >= 1"):
            HealthcheckSpec(type="callable", target="fn", interval_seconds=-5)

    def test_interval_one_is_valid(self):
        hc = HealthcheckSpec(type="callable", target="fn", interval_seconds=1)
        assert hc.interval_seconds == 1


# ── PolicyHintSpec ───────────────────────────────────────────────────────────


class TestPolicyHintSpec:
    @pytest.mark.parametrize("action", ["allow", "deny", "approve"])
    def test_valid_actions(self, action: str):
        ph = PolicyHintSpec(capability_id="repo.read", recommended_action=action)
        assert ph.recommended_action == action

    def test_invalid_action(self):
        with pytest.raises(ValueError, match="Invalid recommended_action"):
            PolicyHintSpec(capability_id="repo.read", recommended_action="block")

    def test_invalid_capability_id(self):
        with pytest.raises(ValueError, match="Invalid capability ID"):
            PolicyHintSpec(capability_id="bad", recommended_action="allow")

    def test_default_reason(self):
        ph = PolicyHintSpec(capability_id="repo.read", recommended_action="allow")
        assert ph.reason == ""


# ── InstructionSpec ──────────────────────────────────────────────────────────


class TestInstructionSpec:
    @pytest.mark.parametrize("scope", ["global", "agent", "session"])
    def test_valid_scopes(self, scope: str):
        ins = InstructionSpec(
            id="ins1", version="1.0.0", scope=scope, content="Do something"
        )
        assert ins.scope == scope

    def test_invalid_scope(self):
        with pytest.raises(ValueError, match="Invalid instruction scope"):
            InstructionSpec(
                id="ins1", version="1.0.0", scope="local", content="text"
            )

    def test_empty_content(self):
        with pytest.raises(ValueError, match="must not be empty"):
            InstructionSpec(
                id="ins1", version="1.0.0", scope="global", content=""
            )

    def test_whitespace_content(self):
        with pytest.raises(ValueError, match="must not be empty"):
            InstructionSpec(
                id="ins1", version="1.0.0", scope="global", content="   "
            )

    def test_invalid_version(self):
        with pytest.raises(ValueError, match="Invalid semver"):
            InstructionSpec(
                id="ins1", version="bad", scope="global", content="text"
            )

    def test_default_priority(self):
        ins = InstructionSpec(
            id="ins1", version="1.0.0", scope="global", content="text"
        )
        assert ins.priority == 50


# ── CapabilitySpec ───────────────────────────────────────────────────────────


class TestCapabilitySpec:
    def test_valid(self):
        cap = CapabilitySpec(
            id="repo.read", version="1.0.0", description="Read repos"
        )
        assert cap.id == "repo.read"
        assert cap.requires_approval is False
        assert cap.default_grant is True
        assert cap.tools == ()

    def test_invalid_id(self):
        with pytest.raises(ValueError, match="Invalid capability ID"):
            CapabilitySpec(id="bad", version="1.0.0", description="x")

    def test_invalid_version(self):
        with pytest.raises(ValueError, match="Invalid semver"):
            CapabilitySpec(id="repo.read", version="nope", description="x")

    def test_with_tools(self):
        cap = CapabilitySpec(
            id="repo.read",
            version="1.0.0",
            description="Read",
            tools=("search", "list"),
        )
        assert cap.tools == ("search", "list")


# ── WorkflowSpec ─────────────────────────────────────────────────────────────


class TestWorkflowSpec:
    def test_valid(self):
        wf = WorkflowSpec(
            id="wf1", version="2.0.0", name="WF", description="A workflow"
        )
        assert wf.steps == ()
        assert wf.required_capabilities == ()

    def test_invalid_version(self):
        with pytest.raises(ValueError, match="Invalid semver"):
            WorkflowSpec(id="wf1", version="x", name="WF", description="d")

    def test_invalid_required_capability(self):
        with pytest.raises(ValueError, match="Invalid capability ID"):
            WorkflowSpec(
                id="wf1",
                version="1.0.0",
                name="WF",
                description="d",
                required_capabilities=("BAD",),
            )

    def test_multiple_capabilities_validated(self):
        wf = WorkflowSpec(
            id="wf1",
            version="1.0.0",
            name="WF",
            description="d",
            required_capabilities=("repo.read", "shell.exec"),
        )
        assert wf.required_capabilities == ("repo.read", "shell.exec")


# ── ToolContribution ─────────────────────────────────────────────────────────


class TestToolContribution:
    def test_valid(self):
        tc = ToolContribution(name="search", description="Search things")
        assert tc.name == "search"
        assert tc.parameters == {}
        assert tc.handler_ref == ""
        assert tc.capability == ""
        assert tc.side_effects == "none"
        assert tc.required_tier == "public"
        assert tc.timeout_seconds == 60.0
        assert tc.retries == 0

    def test_handler_ref(self):
        tc = ToolContribution(
            name="t", description="d", handler_ref="pkg.mod:func"
        )
        assert tc.handler_ref == "pkg.mod:func"

    @pytest.mark.parametrize("se", ["none", "read", "write"])
    def test_valid_side_effects(self, se: str):
        tc = ToolContribution(name="t", description="d", side_effects=se)
        assert tc.side_effects == se

    def test_invalid_side_effects(self):
        with pytest.raises(ValueError, match="Invalid side_effects"):
            ToolContribution(name="t", description="d", side_effects="delete")


# ── ConfigRequirement ────────────────────────────────────────────────────────


class TestConfigRequirement:
    def test_valid(self):
        cr = ConfigRequirement(key="API_KEY")
        assert cr.key == "API_KEY"
        assert cr.type == "string"
        assert cr.required is True
        assert cr.description == ""
        assert cr.default is None

    def test_custom(self):
        cr = ConfigRequirement(
            key="PORT", type="int", required=False, default="8080"
        )
        assert cr.type == "int"
        assert cr.required is False
        assert cr.default == "8080"


# ── PluginSpec ───────────────────────────────────────────────────────────────


class TestPluginSpec:
    def test_valid_minimal(self):
        ps = _minimal_plugin()
        assert ps.id == "my-plugin"
        assert ps.trust_level == "community"

    def test_invalid_id(self):
        with pytest.raises(ValueError, match="Invalid plugin ID"):
            _minimal_plugin(id="Bad Plugin!")

    def test_invalid_version(self):
        with pytest.raises(ValueError, match="Invalid semver"):
            _minimal_plugin(version="nope")

    @pytest.mark.parametrize("st", sorted(SOURCE_TYPES))
    def test_valid_source_types(self, st: str):
        ps = _minimal_plugin(source_type=st)
        assert ps.source_type == st

    def test_invalid_source_type(self):
        with pytest.raises(ValueError, match="Invalid source_type"):
            _minimal_plugin(source_type="docker")

    @pytest.mark.parametrize("rt", sorted(RUNTIME_TYPES))
    def test_valid_runtime_types(self, rt: str):
        ps = _minimal_plugin(runtime_type=rt)
        assert ps.runtime_type == rt

    def test_invalid_runtime_type(self):
        with pytest.raises(ValueError, match="Invalid runtime_type"):
            _minimal_plugin(runtime_type="unknown_rt")

    @pytest.mark.parametrize("tl", TRUST_LEVELS)
    def test_valid_trust_levels(self, tl: str):
        ps = _minimal_plugin(trust_level=tl)
        assert ps.trust_level == tl

    def test_invalid_trust_level(self):
        with pytest.raises(ValueError, match="Invalid trust_level"):
            _minimal_plugin(trust_level="root")

    def test_frozen(self):
        ps = _minimal_plugin()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ps.id = "other"  # type: ignore[misc]

    # convenience properties

    def test_tool_names(self):
        ps = _minimal_plugin(
            tools=(
                ToolContribution(name="a", description="d"),
                ToolContribution(name="b", description="d"),
            )
        )
        assert ps.tool_names == ("a", "b")

    def test_capability_ids(self):
        ps = _minimal_plugin(
            capabilities=(
                CapabilitySpec(id="repo.read", version="1.0.0", description="d"),
            )
        )
        assert ps.capability_ids == ("repo.read",)

    def test_workflow_ids(self):
        ps = _minimal_plugin(
            workflows=(
                WorkflowSpec(
                    id="wf1", version="1.0.0", name="W", description="d"
                ),
            )
        )
        assert ps.workflow_ids == ("wf1",)

    def test_empty_convenience_properties(self):
        ps = _minimal_plugin()
        assert ps.tool_names == ()
        assert ps.capability_ids == ()
        assert ps.workflow_ids == ()


# ── PluginStatus ─────────────────────────────────────────────────────────────


class TestPluginStatus:
    def test_defaults(self):
        st = PluginStatus(plugin_id="my-plugin")
        assert st.state == "discovered"
        assert st.error is None
        assert st.enabled is False

    def test_mutable(self):
        st = PluginStatus(plugin_id="my-plugin")
        st.state = "active"
        st.enabled = True
        assert st.state == "active"
        assert st.enabled is True

    @pytest.mark.parametrize(
        "state",
        [
            "discovered",
            "installed",
            "enabled",
            "active",
            "unhealthy",
            "disabled",
            "failed",
        ],
    )
    def test_valid_states(self, state: str):
        st = PluginStatus(plugin_id="p", state=state)
        assert st.state == state

    def test_invalid_state(self):
        with pytest.raises(ValueError, match="Invalid plugin state"):
            PluginStatus(plugin_id="p", state="exploded")


# ── SOURCE_TYPES / RUNTIME_TYPES / TRUST_LEVELS ─────────────────────────────


class TestConstants:
    def test_source_types(self):
        assert SOURCE_TYPES == frozenset({
            "local", "git", "pip", "builtin",
            "npm", "cargo", "uv", "registry",
        })

    def test_runtime_types(self):
        assert RUNTIME_TYPES == frozenset({
            "native", "cli", "sdk", "mcp", "service", "content",
            "npx", "wasm", "docker", "grpc",
        })

    def test_trust_levels(self):
        assert TRUST_LEVELS == ("builtin", "verified", "community", "untrusted")

# Deprecation Plan: Legacy Tool System → Plugin Platform

## Overview

The legacy tool provider system (`obscura/tools/providers/`, `ToolSpec`, `ToolProviderRegistry`) is being replaced by the new plugin platform (`obscura/plugins/`). The new system introduces YAML manifests, a capability model, policy enforcement, health checks, and a structured loader pipeline—replacing ad-hoc Python-module providers with a declarative, auditable architecture.

This document tracks the migration timeline, provides guidance for authors, and defines the code-removal checklist.

---

## What Is Deprecated

| Deprecated Module | Location | Replacement | Status |
|---|---|---|---|
| `ToolSpec` dataclass | `obscura/core/types.py:194` | `PluginToolSpec` / `ToolContribution` in `obscura/plugins/models.py` | **Active — shim in place** |
| `ToolProvider` protocol | `obscura/tools/providers/__init__.py:35` | `PluginAdapter` protocol in `obscura/plugins/adapters/base.py` | **Active — shim in place** |
| `ToolProviderRegistry` | `obscura/tools/providers/__init__.py:163` | `PluginRegistryService` in `obscura/plugins/registry.py` | **Active — shim in place** |
| `register_plugins()` | `obscura/tools/plugin_loader.py:157` | `PluginLoader.load_all()` in `obscura/plugins/loader.py` | **Active — shim in place** |
| `PluginRegistry` (JSON) | `obscura/tools/plugin_registry.py:24` | `PluginRegistryService` in `obscura/plugins/registry.py` | **Active — CLI fallback** |
| Individual provider files | `obscura/tools/providers/*.py` (12 files) | YAML manifests in `obscura/plugins/builtins/` (13 files) | **Manifests written** |
| Conditional provider wiring | `obscura/agent/agents.py:~403-470` | `PluginLoader` auto-discovery + `ToolBroker` | **Partial** |

---

## Migration Timeline

### Phase A — Shim Layer *(current)*

`ManifestToolProvider` (in `obscura/plugins/loader.py`) bridges old and new systems. The new loader discovers YAML manifests, builds `PluginSpec` instances, and wraps them in a provider that satisfies the existing `ToolProvider` protocol. This lets `ToolProviderRegistry.install_all()` continue to work unchanged.

**Exit criteria:** All 13 builtin manifests load successfully; no provider falls back to legacy Python-only path.

### Phase B — Deprecation Warnings

Add `warnings.warn(..., DeprecationWarning)` to:

- `obscura/tools/plugin_loader.py` — on import and on `register_plugins()` call
- `obscura/tools/plugin_registry.py` — on import and on `PluginRegistry()` instantiation
- `obscura/tools/providers/__init__.py` — on `ToolProviderRegistry()` instantiation
- Each file in `obscura/tools/providers/` — on direct import

**Exit criteria:** CI logs show zero deprecation warnings from internal code; only external consumers trigger them.

### Phase C — Feature Flag Period

Introduce `OBSCURA_LEGACY_PROVIDERS=1` environment variable:

- When **set**, the old `register_plugins()` path runs and the legacy `PluginRegistry` is used by the CLI.
- When **unset** (default), only `PluginLoader.load_all()` runs.

The CLI `/plugin` command already has fallback logic (`commands.py:2471-2482`) that tries the new registry first, then falls back to legacy. During this phase, the fallback is gated behind the flag.

**Exit criteria:** The flag has been unset in production for ≥ 2 release cycles with no regressions.

### Phase D — Code Removal

Delete all deprecated modules (see [Code Removal Checklist](#code-removal-checklist) below).

---

## Migration Guide

### For Provider Authors

**Before (Python provider):**

```python
# obscura/tools/providers/websearch.py
from obscura.core.types import ToolSpec
from obscura.tools.providers import ToolProvider, ToolProviderContext

class WebSearchProvider(ToolProvider):
    async def install(self, ctx: ToolProviderContext) -> None:
        ctx.agent.tool_registry.register(ToolSpec(
            name="web_search",
            description="Search the web",
            parameters={"query": {"type": "string"}},
            handler=self._search,
            required_tier="public",
            side_effects="read",
            timeout_seconds=30.0,
        ))

    async def uninstall(self, ctx: ToolProviderContext) -> None:
        ctx.agent.tool_registry.unregister("web_search")
```

**After (YAML manifest):**

```yaml
# obscura/plugins/builtins/websearch.yaml
id: websearch
name: Web Search
version: "1.0.0"
runtime_type: native
trust_level: builtin

capabilities:
  - id: search.web
    version: "1.0.0"
    description: Search the public web
    tools: [web_search]
    requires_approval: false
    default_grant: true

tools:
  - name: web_search
    description: Search the web
    parameters:
      query: { type: string }
    handler_ref: "obscura.tools.providers.websearch:search"
    capability: search.web
    side_effects: read
    required_tier: public
    timeout_seconds: 30.0
```

**Steps:**

1. Create a YAML manifest file describing the plugin metadata, capabilities, and tools.
2. Set `handler_ref` to the dotted import path of the handler function (e.g., `module.path:function`).
3. Add any required config via `config_requirements` (replaces hard-coded env-var checks).
4. If the plugin needs pip/system dependencies, add a `bootstrap` section.
5. Place the manifest in `obscura/plugins/builtins/` (for builtins) or `~/.obscura/plugins/` (for local).

### For Agent Manifest Authors

The new capability model gates tool visibility per-agent:

| Concept | Description |
|---|---|
| **Capability** | A named permission surface (e.g., `search.web`) that groups one or more tools. |
| **Grant** | An agent is *granted* a capability, making its tools visible and callable. |
| **Approval** | Capabilities with `requires_approval: true` prompt the user before first use. |
| **Default grant** | Capabilities with `default_grant: true` are available to all agents without explicit grant. |

Use `/capability grant <cap> --agent <id>` to assign capabilities. The `CapabilityResolver` checks grants at execution time via the `ToolBroker`.

### For CLI Users

| Action | Old Command | New Command |
|---|---|---|
| List plugins | `/plugin list` | `/plugin list` *(same, uses new registry)* |
| Install plugin | `/plugin install <source>` | `/plugin install <source>` *(same)* |
| Remove plugin | `/plugin remove <name>` | `/plugin remove <name>` *(same)* |
| Enable/disable | *not available* | `/plugin enable <id>` / `/plugin disable <id>` |
| Plugin details | *not available* | `/plugin info <id>` |
| Health status | *not available* | `/plugin health` |
| List capabilities | *not available* | `/capability list` |
| Grant capability | *not available* | `/capability grant <cap> --agent <id>` |
| Deny capability | *not available* | `/capability deny <cap> --agent <id>` |
| Check capability | *not available* | `/capability check <cap> --agent <id>` |

---

## Shim Layer Strategy

The `ManifestToolProvider` class (`obscura/plugins/loader.py`) acts as the bridge:

```
YAML Manifest
  → PluginLoader.discover_builtins()   # parse YAML → PluginSpec
  → PluginLoader._validate()           # schema check
  → PluginLoader._check_config()       # env var resolution
  → PluginLoader._bootstrap()          # dependency install
  → ManifestToolProvider(spec)          # wraps PluginSpec as ToolProvider
  → provider_registry.add(provider)    # old ToolProviderRegistry accepts it
  → install_all()                      # calls provider.install()
      → converts ToolContribution → ToolSpec
      → registers with agent.tool_registry
```

`ManifestToolProvider.install()` iterates over `spec.tools`, resolves each `handler_ref` via dynamic import, and constructs a `ToolSpec` for backward compatibility with `ToolRegistry`. Once the shim is removed (Phase D), the `ToolBroker` will handle execution directly.

---

## Code Removal Checklist

> Complete all items before merging the removal PR.

- [ ] Remove `obscura/tools/providers/` directory (12 provider files + `__init__.py`)
- [ ] Remove `obscura/tools/plugin_loader.py`
- [ ] Remove `obscura/tools/plugin_registry.py`
- [ ] Remove conditional provider wiring blocks in `obscura/agent/agents.py` (~lines 403-470)
- [ ] Remove `ToolProvider` protocol from `obscura/tools/providers/__init__.py`
- [ ] Remove `ToolProviderRegistry` from `obscura/tools/providers/__init__.py`
- [ ] Update all imports referencing deprecated modules
- [ ] Remove `OBSCURA_LEGACY_PROVIDERS` feature flag and gating logic
- [ ] Remove `ManifestToolProvider` shim (no longer needed once `ToolBroker` is the sole executor)
- [ ] Remove legacy fallback path in `/plugin` CLI command (`commands.py:2471-2482`)
- [ ] Run full test suite and verify zero import errors
- [ ] Verify all 13 builtin manifests load via `PluginLoader.load_all()`

---

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| External consumers depend on `ToolProvider` protocol | Phase B deprecation warnings give advance notice; Phase C flag allows opt-in to legacy path. |
| Manifest parsing regression breaks tool loading | `ManifestToolProvider` shim validates at startup; `PluginLoader` emits structured errors to `obscura/plugins/observability.py`. |
| Missing env vars disable plugins silently | `PluginLoader._check_config()` sets state to `"disabled"` with a logged reason; `/plugin health` surfaces it. |
| Bootstrap (pip install) fails in constrained envs | `BootstrapSpec` supports multiple strategies (pip, uv, binary); failure sets state to `"failed"`, not crash. |
| Rollback needed after Phase C | Set `OBSCURA_LEGACY_PROVIDERS=1` to re-enable the old path entirely. No code changes required. |
| Incomplete test coverage masks breakage | **Requirement:** ≥ 95% line coverage on `obscura/plugins/` before Phase D removal PR is merged. |
| Capability grants break existing agent workflows | All builtin capabilities default to `default_grant: true`, preserving current behavior unless explicitly restricted. |

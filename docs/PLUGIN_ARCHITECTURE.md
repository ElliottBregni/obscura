# Obscura Plugin Architecture

## Overview

The Obscura plugin platform allows installable packages to contribute normalized
capabilities, tools, workflows, and instructions to a governed agent runtime.
Plugins are declarative resource bundles — they describe what they contribute
through a manifest; the runtime decides what to load, grant, and execute.

This document defines the platform vocabulary, layer responsibilities, and
the contracts between them.

---

## Platform Vocabulary

### Plugin

**Is:** An installable package that contributes normalized resources (capabilities,
tools, workflows, instructions) to the Obscura runtime through a declarative manifest.

**Is not:** Arbitrary code that can directly mutate runtime state, inject hidden
prompts, or bypass policy enforcement.

A plugin is identified by a unique `id` (e.g. `obscura-github`), carries a semver
`version`, declares a `trust_level`, and lists the resources it contributes. Plugins
may be sourced from pip packages, git repositories, local directories, or built into
the core.

### Capability

**Is:** A named, permissioned feature surface (e.g. `repo.read`, `shell.exec`) that
gates access to one or more tools.

**Is not:** A tool itself — it is a grouping and authorization mechanism that the
policy engine evaluates before tools become visible to an agent.

Capabilities follow a dot-separated naming convention (`<domain>.<action>`). Agents
are granted capabilities explicitly; tools are only visible through granted capabilities.
This provides least-privilege behavior without per-tool configuration.

### Tool

**Is:** A concrete callable function with typed JSON Schema input/output, policy
enforcement, and audit logging, registered in the tool registry and executed through
the tool broker.

**Is not:** Allowed to bypass the broker, execute outside the audit trail, or
operate without schema validation. All tool execution — regardless of origin —
flows through a single choke point.

Tools are defined by `ToolSpec` (see `obscura/core/types.py`) and belong to one
or more capabilities. A tool's `side_effects` field (`none`, `read`, `write`)
informs the policy engine and approval flow.

### Workflow

**Is:** A higher-level composed behavior built from tools and capabilities —
a reusable, named sequence of steps that an agent or user can invoke.

**Is not:** A plugin. Workflows are resources that plugins contribute. They
cannot define new tools or grant capabilities; they compose existing ones.

Workflows declare `required_capabilities` and are only executable when all
required capabilities are granted to the executing agent.

### Instruction Overlay

**Is:** Optional guidance text that a plugin contributes to agent system prompts,
scoped to a context (global, agent, or session) and ordered by priority.

**Is not:** A system prompt replacement. Instruction overlays are additive and
assembled by the prompt assembler alongside the agent's own system prompt.
Plugins cannot use instructions to override core behavior or inject hidden state.

### Policy Hint

**Is:** A plugin's recommended access rules for its capabilities — advisory
metadata that the policy engine may consider when no explicit user/org policy exists.

**Is not:** Self-granting. Plugins cannot enforce their own policy hints. The
policy engine is the sole authority on access decisions.

---

## Layer Responsibilities

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI / API Layer                          │
│  /plugin list|install|remove|enable|disable|info|health         │
│  /capability list|grant|deny|check                              │
│  /tool list                                                     │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                      Plugin Loader                              │
│  discover → validate → resolve_config → bootstrap →             │
│  normalize → register → track_health                            │
│                                                                 │
│  Adapters: native | cli | sdk | mcp | service | content         │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                   Runtime Registries                             │
│  ┌──────────┐ ┌────────────┐ ┌───────┐ ┌──────────┐ ┌────────┐│
│  │ Plugins  │ │Capabilities│ │ Tools │ │Workflows │ │Instrns ││
│  └──────────┘ └────────────┘ └───────┘ └──────────┘ └────────┘│
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                   Governance Layer                               │
│  ┌──────────────┐  ┌───────────────────┐  ┌──────────────────┐ │
│  │ Policy Engine│  │Capability Resolver│  │  Approval Flow   │ │
│  └──────────────┘  └───────────────────┘  └──────────────────┘ │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                      Tool Broker                                │
│  validate → policy_check → approve → route → execute →          │
│  normalize_result → audit → return                              │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                    Observability                                 │
│  AuditEvent │ Trace Log │ OTel Spans │ Plugin Events            │
└─────────────────────────────────────────────────────────────────┘
```

### What Each Layer Does and Does Not Do

| Layer | Responsible For | Not Responsible For |
|-------|----------------|---------------------|
| **Plugin Loader** | Discovering, validating, and loading plugin manifests; normalizing resources; managing lifecycle states | Executing tools, enforcing policy, granting capabilities |
| **Runtime Registries** | Indexing normalized resources for fast lookup and filtering by agent/session/capability | Validation, policy decisions, or execution |
| **Governance** | Evaluating access decisions, resolving capability grants, managing approval workflows | Loading plugins, storing resources, executing tools |
| **Tool Broker** | Executing tools through a single audited path with validation, policy, timeout, retry, and error normalization | Discovering plugins, managing capability grants, persisting state |
| **Observability** | Recording structured events for audit, debugging, and replay | Making access decisions or modifying runtime behavior |

---

## Resource Flow

A plugin contributes resources through this pipeline:

```
plugin.yaml manifest
    │
    ▼
Manifest Validator ─── rejects invalid manifests
    │
    ▼
Plugin Adapter (native/cli/sdk/mcp/service/content)
    │
    ▼
Normalized Resources (PluginSpec, CapabilitySpec, ToolSpec, etc.)
    │
    ▼
Runtime Registries (indexed, queryable)
    │
    ▼
Capability Resolver (agent starts → resolves grants → filters visible tools)
    │
    ▼
Tool Broker (agent calls tool → validate → policy → execute → audit)
```

---

## Trust Model

Plugins declare a `trust_level`:

| Level | Meaning | Default Policy |
|-------|---------|---------------|
| `builtin` | Ships with Obscura core | Allow all capabilities |
| `verified` | Reviewed and signed by Obscura team | Allow, may require approval for write capabilities |
| `community` | Published by third parties, unreviewed | Require explicit grant per capability |
| `untrusted` | Unknown source | Deny by default |

The policy engine uses trust level as one input. Explicit user/org policies always
override trust-level defaults.

---

## Configuration Precedence

Plugin configuration resolves in order:

1. Environment variables (highest priority)
2. `~/.obscura/settings.json` plugin overrides
3. Plugin manifest `config` defaults (lowest priority)

---

## Versioning

Every resource model carries a `version` field (semver). The loader validates
version compatibility and the registry tracks installed versions. Future work
includes version pinning and dependency resolution between plugins.

---

## Integration Matrix (YAML + Registry)

The following integrations are wired through builtin YAML specs and installed
in the plugin registry (`.obscura/plugins/registry.json`).

| Integration | Plugin ID | `yaml_spec` | `registry_installed` |
|---|---|---|---|
| SecurityTrails API | `securitytrails` | yes | yes |
| Censys API | `censys` | yes | yes |
| Shodan API | `shodan` | yes | yes |
| Polygon.io | `polygon` | yes | yes |
| Alpha Vantage | `alphavantage` | yes | yes |
| SEC EDGAR API | `sec-edgar` | yes | yes |
| Sentinel Hub | `sentinel-hub` | yes | yes |
| MarineTraffic API | `marinetraffic` | yes | yes |
| FlightAware API | `flightaware` | yes | yes |
| Playwright SDK/CLI | `playwright` | yes | yes |
| Browserless | `browserless` | yes | yes |
| DuckDB | `duckdb` | yes | yes |
| Apache Arrow / DataFusion | `datafusion` | yes | yes |
| ripgrep | `ripgrep` | yes | yes |
| jq | `jq` | yes | yes |
| fzf | `fzf` | yes | yes |
| fd | `fd` | yes | yes |
| Wikidata API | `wikidata` | yes | yes |
| OpenAlex | `openalex` | yes | yes |
| GitHub GraphQL API | `github-graphql` | yes | yes |
| Docker Engine API | `docker-engine` | yes | yes |
| Kubernetes API | `kubernetes-api` | yes | yes |
| Prometheus API | `prometheus` | yes | yes |
| Grafana API | `grafana` | yes | yes |
| Matrix API | `matrix` | yes | yes |
| NATS | `nats` | yes | yes |
| OpenStreetMap Overpass API | `overpass` | yes | yes |

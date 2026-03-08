# Obscura Plugin Boundaries

## Purpose

This document defines the hard boundaries for the Obscura plugin system.
These constraints are enforced by the manifest validator, plugin loader,
tool broker, and policy engine. They exist to prevent platform drift,
maintain security, and ensure observability.

---

## Plugins MUST NOT

### 1. Inject Hidden Prompt State

Plugins contribute instruction overlays through the manifest. These overlays
are visible to the prompt assembler and logged in the audit trail. Plugins
cannot inject text into agent system prompts through any other path.

**Enforcement:** The prompt assembler only reads from the instruction registry.
Plugin code has no access to prompt construction internals.

### 2. Bypass Policy

All tool execution flows through the tool broker, which evaluates policy
before every call. Plugins cannot execute tools directly, call external
APIs outside the broker path, or suppress policy evaluation.

**Enforcement:** Tool handlers are registered as callables but only invoked
by the broker. The broker is the sole execution entry point.

### 3. Register Untyped or Unvalidated Tools

Every tool must have a valid JSON Schema for its parameters and a declared
`side_effects` value. Tools without schema are rejected at manifest validation
time. Runtime tool registration outside the manifest path is blocked for
non-builtin plugins.

**Enforcement:** Manifest validator rejects tools without parameter schemas.
The loader refuses to register tools that fail schema validation.

### 4. Perform Unrestricted Shell/Network/File Actions

Plugins that need shell, network, or filesystem access must declare the
corresponding capabilities in their manifest. The policy engine gates these
capabilities. Plugins cannot spawn subprocesses, open network connections,
or read/write files except through declared, broker-executed tools.

**Enforcement:** Plugin adapters (CLI, SDK, service) constrain execution
paths. The broker logs all I/O. Future: sandbox profiles restrict system calls.

### 5. Self-Grant Capabilities

Plugins declare capabilities and policy hints. They cannot grant capabilities
to agents or sessions. Only the policy engine, responding to explicit user/org
policy or admin action, can grant capabilities.

**Enforcement:** The capability resolver reads grants from the policy store,
not from plugin manifests. Policy hints are advisory only.

### 6. Mutate Agent Memory Without Explicit Pathways

Plugins that need to read or write agent memory must declare memory-related
capabilities and use the memory tool provider's registered tools. Direct
access to the vector store or memory backend is not permitted.

**Enforcement:** Memory tools are registered through the MemoryToolProvider.
Plugin code does not receive references to memory backends.

### 7. Create Side Effects Outside Audited Execution Paths

Every tool call, approval decision, capability grant, and plugin lifecycle
event is recorded in the audit trail. Plugins cannot perform actions that
bypass this trail. Background work, scheduled tasks, and event handlers
must all route through the broker.

**Enforcement:** The broker emits audit events for every execution. The
observability layer tracks plugin lifecycle events independently.

### 8. Modify Other Plugins' Resources

Plugins can only contribute their own resources (tools, capabilities,
workflows, instructions). They cannot modify, override, or remove
resources contributed by other plugins.

**Enforcement:** The runtime registries track resource ownership by
plugin ID. Registration calls that conflict with existing resources
from a different plugin are rejected.

### 9. Execute Arbitrary Code at Manifest Parse Time

The plugin manifest (`plugin.yaml`) is a declarative document. It is
parsed and validated without executing any plugin code. Bootstrap hooks
run only after validation succeeds and only in the loader pipeline.

**Enforcement:** The manifest parser uses safe YAML loading. Handler
references are resolved lazily during the loader's normalize step,
not during parsing.

### 10. Bypass Trust Level Restrictions

A plugin's `trust_level` constrains what the default policy allows.
Plugins cannot self-declare a higher trust level than their source
warrants. Trust levels are assigned by the registry based on source:

- `builtin`: Only for plugins shipped in the Obscura core package
- `verified`: Assigned by the registry when a verified signature is present
- `community`: Default for pip/git-installed packages
- `untrusted`: Default for local/unknown sources

**Enforcement:** The registry assigns trust level based on source type
and signature verification, ignoring the plugin's self-declared level
for non-builtin sources.

---

## Plugins MUST

### 1. Declare All Contributions in the Manifest

Tools, capabilities, workflows, instructions, and config requirements
must all be declared in `plugin.yaml`. Undeclared resources are not
loaded.

### 2. Handle Failures Gracefully

Plugin `install()` and tool handlers must not raise unhandled exceptions
that crash the runtime. The loader wraps all plugin operations in
try/except and transitions failed plugins to the `unhealthy` state.

### 3. Declare Side Effects Accurately

Tools must declare `side_effects` as `none`, `read`, or `write`.
Misrepresenting side effects (e.g. declaring `none` for a tool that
writes to external systems) is a policy violation and grounds for
trust level downgrade.

### 4. Support Health Checks (Recommended)

Plugins should declare a `healthcheck` in their manifest so the
runtime can detect and report degraded plugins. Plugins without
healthchecks are assumed healthy, which may mask failures.

### 5. Use Semver for Versioning

Plugin versions must follow semantic versioning. The registry uses
version information for upgrade decisions and compatibility checks.

---

## Enforcement Summary

| Boundary | Enforced By |
|----------|------------|
| No hidden prompts | Prompt assembler reads only instruction registry |
| No policy bypass | Tool broker is sole execution entry point |
| No untyped tools | Manifest validator + loader |
| No unrestricted I/O | Capability gating + broker logging |
| No self-granting | Capability resolver reads policy store only |
| No memory mutation | Memory tools are the only pathway |
| No unaudited side effects | Broker emits audit events for all executions |
| No cross-plugin mutation | Registry tracks resource ownership by plugin ID |
| No code at parse time | Safe YAML loading; lazy handler resolution |
| No trust escalation | Registry assigns trust based on source + signature |

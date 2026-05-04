# Refactor Plan — String Literals → StrEnum, Dict Args → Pydantic Models

> **Status (Rounds 1+2+3 complete).** Foundation, Round 1 (~60 enum migrations into `core/enums/`), Round 2 (Pydantic record/boundary/discriminated-union models in `core/models/`), and Round 3 (back-compat shim deletion across agent/auth/lifecycle/error/protocol/messaging/tools/storage/ui domains) all merged. Pyright errors stable at ~87, all in user-territory in-flight work (agent_loop_v2.py, eval/engine.py, etc.). The ANN401 ruff rule banning `dict[str, Any]` in `obscura/core/` is *not* yet wired in CI — there are 234 existing `dict[str, Any]` sites in core/ that would need migration first; the convention is documented here and enforced via code review until a future round migrates the existing call sites.

**Goal.** Eliminate loose string literals (used as dict keys, in conditionals, or in `Literal[...]` annotations) by replacing them with `StrEnum`s, and replace untyped `dict[str, Any]` parameters with `pydantic.BaseModel` subclasses (composed via mixins). Pyright enforces it module by module.

**Source of truth.** Existing inventory: 54 enum classes across 27 files (some duplicated), ~500+ conditional string-literal sites in 15 domains, 22 dict-shape patterns (mix of internal-state and wire-format), 18 promotable `Literal[]` aliases.

---

## 1. Scope

**In scope**
- All internal-shape `dict[str, Any]` parameters and return types in `obscura/`.
- All string-keyed conditionals (`==`, `!=`, `in`, `match`, `startswith` for category dispatch).
- All `Literal["a", "b", ...]` aliases that aren't tied to a wire format.
- Tagged unions currently expressed as `dict["kind"]` dispatch.

**Out of scope (stay strings)**
- Wire formats: JSON-RPC envelope keys, MCP method names on the wire, OpenAI/Anthropic API request bodies, A2A protocol envelopes.
- HTTP header names (`Authorization`, `Content-Type`, …) — these are a public standard.
- POSIX exit codes (stay `int`).
- Filesystem path components, env var names, log-line keys for grep-ability.

The wire-format stuff is *parsed into* Pydantic models at I/O boundaries; internal code never touches the raw dict.

---

## 2. Target Architecture

### 2.1 New packages

```
obscura/core/
├── enums/
│   ├── __init__.py           # curated re-exports — single import site
│   ├── _base.py              # Lifecycle protocol, helpers (is_terminal, parse_lenient)
│   ├── agent.py              # AgentEventKind, AgentPhase, AgentType, ExecutionMode, APERMode
│   ├── auth.py               # PermissionMode, ApprovalStatus, CapabilityTier, VerdictKind, AuthProvider
│   ├── error.py              # ErrorCategory (unified — see §3.1), TransientErrorKind
│   ├── lifecycle.py          # SessionStatus, TaskStatus, GoalStatus, WorktreeStatus, PlanStatus, …
│   ├── messaging.py          # ChannelMode, TriggerKind, MessageRole, PushProvider
│   ├── protocol.py           # MCPMethod, MCPLogLevel, MCPTransport, A2APartKind, A2ARole, JSONRPCErrorCode
│   ├── storage.py            # DBBackendType, MemoryType, MemoryEventKind, MemorySource
│   ├── tools.py              # ToolErrorType, BashRisk, ToolChoiceMode, SideEffects, ContentBlockKind, HTTPMethod
│   └── ui.py                 # OutputMode, LogFormat, TUIMode, BannerTheme, BorderStyle, DiffLineType, UIMode
└── models/
    ├── __init__.py
    ├── _base.py              # ObscuraModel (frozen, strict, extra=forbid)
    ├── _mixins.py            # TimestampedMixin, IdentifiedMixin, MetadataMixin, StatusedMixin[S]
    ├── agent.py              # AgentConfig, AgentSpec, ExecutionRequest, AgentEvent
    ├── content.py            # ContentBlock discriminated union (Text|Thinking|ToolUse|ToolResult)
    ├── lifecycle.py          # TaskRecord, GoalRecord, ApprovalRecord, WorktreeEntry, SessionRecord, HealthReport
    ├── protocol.py           # JSONRPCRequest/Response, MCPMessage, A2APart union, A2ATaskMessage union
    ├── tools.py              # ToolSpec, ToolCall, ToolResult, ToolChoice, BashClassification
    └── triggers.py           # Trigger discriminated union (IMessage|Message|Email|Stop)
```

The 54 existing enums get **moved** here — every old import path becomes a one-line re-export for one release cycle, then deleted in Phase 9.

Dependency DAG (one-way, enforced by pyright import-cycle detection):
`models → enums + mixins`. Enums never import models.

### 2.2 Naming & conventions

- `StrEnum` (Python ≥3.11 — repo is 3.13+). Member names `UPPER_SNAKE`, values `lower_snake` matching today's wire strings — keeps wire format and persisted state byte-identical.
- All Pydantic models inherit:
  ```python
  class ObscuraModel(BaseModel):
      model_config = ConfigDict(
          frozen=True,
          extra="forbid",
          strict=True,
          validate_assignment=True,
          use_enum_values=False,
      )
  ```
  Mutable records (long-lived rows like `TaskRecord`) drop `frozen=True` only.
- Discriminated unions: `kind: <Enum>` field + `Annotated[Union[...], Field(discriminator="kind")]`.
- Boundary models reading external/persisted JSON use `extra="ignore"` to tolerate forward compat; in-memory models keep `extra="forbid"`.

### 2.3 Mixins (composition — not inheritance trees)

```python
class TimestampedMixin(BaseModel):
    created_at: datetime
    updated_at: datetime

class IdentifiedMixin(BaseModel):
    id: ULID

class MetadataMixin(BaseModel):
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, str] = Field(default_factory=dict)

class StatusedMixin[S: StrEnum](BaseModel):
    status: S
    status_changed_at: datetime
```

`TaskRecord(IdentifiedMixin, TimestampedMixin, StatusedMixin[TaskStatus])` — the status enum is parameterized, so reuse is real (one mixin, many enums). Same template for Goal, Approval, Worktree, Session, Health — one mixin handles all six.

### 2.4 Discriminated unions replace `dict["kind"]` dispatch

Before:
```python
if block["kind"] == "tool_use":
    handle_tool_use(block)
elif block["kind"] == "tool_result":
    handle_tool_result(block)
```

After:
```python
match block:
    case ToolUseBlock(): handle_tool_use(block)
    case ToolResultBlock(): handle_tool_result(block)
    case _ as unreachable: assert_never(unreachable)
```

The `assert_never` makes pyright the source of exhaustiveness — adding a new variant without a case fails CI.

### 2.5 Dispatch maps use enum keys

```python
HANDLERS: dict[EventKind, Handler] = {
    EventKind.TEXT_DELTA: handle_text,
    EventKind.TOOL_CALL: handle_tool_call,
}
```
No string keys.

### 2.6 SQL boundaries

`status = TaskStatus.PENDING.value` at the bind site. Never `WHERE status = 'pending'` inline. Wrap in helpers if it shows up in many queries.

### 2.7 Wire-format boundary pattern

```python
@app.post("/mcp")
async def handle(raw: dict[str, Any]) -> dict[str, Any]:
    request = JSONRPCRequest.model_validate(raw)   # dict → model (validates)
    response = await dispatch(request)              # internal: model only
    return response.model_dump(by_alias=True)       # model → dict on egress
```

Internal code never sees `raw`. The cast point is the only place that knows wire-format string keys.

---

## 3. Inventory & Reuse Map

### 3.1 Existing enum problems to fix

| Issue | Location | Resolution |
|---|---|---|
| `ErrorCategory` defined 3× with disjoint members | `core/agent_loop.py:540`, `core/kairos/errors.py:8`, `core/supervisor/errors.py:13` | Single `ErrorCategory` in `enums/error.py`. Members prefixed by sub-domain (`KAIROS_GOAL`, `SUPERVISOR_LOCK_CONTENTION`, `AGENT_TRANSIENT`, …). Old names re-export. |
| 7 lifecycle enums share PENDING/ACTIVE/COMPLETED/FAILED | `GoalStatus`, `PlanStatus`, `TaskStatus`, `SessionStatus`, `AgentStatus`, `LazyState`, `SupervisorState` | Each remains its own enum (semantics differ). All conform to a `Lifecycle` Protocol with `is_terminal()`, `is_active()` helpers. |
| 38 plain `Enum`s (not `StrEnum`) | various | Convert to `StrEnum` where serialized to JSON or compared with strings. Keep `IntEnum.Priority` (numeric ordering required). |
| 3 large event-kind enums (Agent, Kairos, Supervisor — 21/20/22 members) | `core/types.py`, `core/kairos/types.py`, `core/supervisor/types.py` | Keep separate (different streams). Inherit a small `EventKindBase` for shared `.is_error_event()` / `.is_terminal()`. |

### 3.2 New enums to create (33)

| Enum | Members | Replaces strings in |
|---|---|---|
| `ContentBlockKind` | TEXT, THINKING, TOOL_USE, TOOL_RESULT | `core/types.py:113` + ~80 conditional sites |
| `AgentType` | LOOP, DAEMON, REACTIVE, SCHEDULED, APER | `tools/swarm.py`, `agent/supervisor.py` |
| `ExecutionMode` | RUN, LOOP, STREAM, STREAM_LOOP, BLOCKING, APER | `schemas/templates.py`, `agent/peers.py`, `routes/agents.py` |
| `MemoryType` | FACT, EPISODE, PREFERENCE, DECISION, SUMMARY, TODO, GENERAL | `tools/memory_tools.py`, `vector_memory/decay.py`, `kairos/user_profile.py` |
| `MemoryEventKind` | SET, DELETE, EXPIRE | `memory/events.py` |
| `MemorySource` | KV, VECTOR | `memory/events.py` |
| `ApprovalStatus` | PENDING, APPROVED, DENIED, EXPIRED | `approvals.py`, `routes/tool_approvals.py` |
| `BackgroundTaskStatus` | RUNNING, COMPLETED, FAILED, STOPPED | `core/background_tasks.py` |
| `WorktreeStatus` | KEPT, ORPHAN, ACTIVE | `tools/worktree*.py` |
| `TaskQueueStatus` | PENDING, COMPLETED, FAILED | `core/task_queue.py` SQL strings |
| `HealthStatus` | OK, DEGRADED, UNAVAILABLE | `core/health.py`, `core/lifecycle_events.py` |
| `CompilerSpecKind` | TEMPLATE, AGENT, POLICY, PACK, WORKSPACE | `core/compiler/specs.py` (5 fields) |
| `A2APartKind` | TEXT, FILE, DATA | `integrations/a2a/types.py` |
| `A2ARole` | USER, AGENT | `integrations/a2a/types.py` |
| `A2ATaskMessageKind` | TASK, STATUS_UPDATE, ARTIFACT_UPDATE | `integrations/a2a/types.py` |
| `PeerKind` | LOCAL, A2A_REMOTE, UNIX_SOCKET | `agent/peers.py` |
| `PushProvider` | APNS, FCM, EXPO | `integrations/push/client.py` |
| `MCPTransport` | STDIO, SSE | `schemas/templates.py` |
| `DiffLineType` | ADD, REMOVE, CONTEXT | `cli/app/diff_engine.py` |
| `ProfileSource` | USER_STATED, INFERRED, OBSERVED | `profile/models.py` |
| `AuthProvider` | GITHUB, GOOGLE | `routes/auth_status.py` |
| `MCPLogLevel` | DEBUG, INFO, NOTICE, WARNING, ERROR, CRITICAL, ALERT, EMERGENCY | `integrations/mcp/types.py` |
| `SideEffects` | NONE, READ, WRITE | `core/types.py:218` ToolSpec, ~20 callsites |
| `ToolChoiceMode` | AUTO, NONE, REQUIRED, FUNCTION | `core/types.py:267` |
| `DBBackendType` | SQLITE, POSTGRESQL, QDRANT | `core/db_factory.py`, `vector_memory/*.py`, `notify/factory.py` |
| `TriggerKind` | IMESSAGE, MESSAGE, EMAIL, STOP | `agent/daemon_agent.py` |
| `VerdictKind` | APPROVE, DENY, ALLOW, BLOCK, ASK, KILL, SUPPRESS | `arbiter/verdicts.py`, `core/hooks.py`, `tools/arbiter_tools.py` |
| `UIMode` | PERMISSION, NOTIFY, QUESTION, MULTI_SELECT | `tools/system/_ui.py` |
| `OutputMode` | CLI, JSON, TEXT | `config.py`, `cli/render.py` |
| `LogFormat` | JSON, TEXT, CONSOLE | `core/config.py` |
| `FilterMode` | ALL, ANY | `vector_memory/vector_memory_filters.py` |
| `ComparisonOperator` | EQ, NE, GT, LT, GTE, LTE, CONTAINS | `vector_memory/vector_memory_filters.py` |
| `HTTPMethod` | GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS | `tools/system/_http.py`, `tools/result.py`, `tools/providers/*.py` |

### 3.3 Pydantic models to create (replacing dicts)

| Model | Replaces dict at | Mixins / Notes |
|---|---|---|
| `ContentBlock` (Annotated Union) | `core/types.py` `ContentBlock.kind: str` | Discriminated on `ContentBlockKind`; 4 variants |
| `AgentEvent` | `core/types.py`, `core/event_store.py` | Currently dataclass — promote; benchmark first (high construction frequency) |
| `AgentConfig` | `tools/swarm.py` `cfg.get(...)` dict access | Typed: `type: AgentType`, `provider: Backend`, `mode: ExecutionMode` |
| `MCPServerSpec` | `~/.obscura/mcp/core.json` raw dict | `transport: MCPTransport`, env vars typed |
| `TaskRecord` | `tools/task_tools.py` `current["status"]` | `IdentifiedMixin + TimestampedMixin + StatusedMixin[TaskStatus]` |
| `GoalRecord` | `tools/goal_tools.py` | `IdentifiedMixin + TimestampedMixin + StatusedMixin[GoalStatus]` |
| `ApprovalRecord` | `approvals.py` | `IdentifiedMixin + TimestampedMixin + StatusedMixin[ApprovalStatus]` |
| `WorktreeEntry` | `tools/worktree_registry.py` | `StatusedMixin[WorktreeStatus]` |
| `SessionRecord` | `core/event_store.py` | `IdentifiedMixin + StatusedMixin[SessionStatus]` |
| `HealthReport` | `core/health.py` | `StatusedMixin[HealthStatus]` |
| `JSONRPCRequest` / `Response` | wire-format dict at MCP boundary | Validate inbound, dump outbound |
| `MCPMessage` | wire-format dict | Discriminated union over `MCPMethod` |
| `A2APart` (Annotated Union) | `integrations/a2a/types.py` | TextPart \| FilePart \| DataPart on `A2APartKind` |
| `A2ATaskMessage` (Annotated Union) | `integrations/a2a/types.py` | Task \| StatusUpdate \| ArtifactUpdate on `A2ATaskMessageKind` |
| `ToolCall` | core/types.py partial dataclass | Promote to BaseModel; args validated against ToolSpec.parameters |
| `ToolResult` | `tools/result.py` builder + dict | Boundary type — internal model + `.model_dump()` at egress |
| `ToolSpec` | partial dataclass today | `side_effects: SideEffects`, `parameters: ParametersSpec` |
| `BashClassification` | `core/bash_classifier.py` | `risk: BashRisk` |
| `HookContext` | dict passed to hook callbacks | Known finite key set |
| `Trigger` (Annotated Union) | `agent/daemon_agent.py` `trigger.kind: str` | IMessage \| Message \| Email \| Stop on `TriggerKind` |
| `SpecKind` family (5 specs) | `core/compiler/specs.py` Literal fields | Discriminated union on `CompilerSpecKind` |

### 3.4 Strings that stay strings

- Wire-protocol envelope keys: JSON-RPC `jsonrpc`, `method`, `params`, `result`, `error`, `id`.
- HTTP header names (`Authorization`, `Content-Type`, …).
- OpenAI/Anthropic API request body keys (`role`, `content`, `tool_calls`, …) — handled by SDK types.
- POSIX exit codes (integer, not enum).
- Env var names, file paths, regexes.
- Tool *result* JSON keys (`ok`, `error`, `data`, `stdout`, `stderr`, …) — wire format consumed by frontend; wrap with Pydantic but `model_dump()` on egress.

---

## 4. Phased Roadmap

Each phase is one PR (or a short stack). Each phase ends green: `make lint && make typecheck && pytest -m "not e2e"`.

### Phase 0 — Foundation (≈1 day)
- Add `obscura/core/enums/` + `obscura/core/models/` with `_base.py`, `_mixins.py`, `__init__.py`.
- Define `ObscuraModel`, `TimestampedMixin`, `IdentifiedMixin`, `MetadataMixin`, `StatusedMixin[S]`.
- Define `Lifecycle` Protocol with `is_terminal()`, `is_active()`.
- Pyright strict for both new packages.
- **Deliverable**: empty packages with infra. Nothing else moves yet.

### Phase 1 — Consolidate the 54 existing enums (≈1 day)
- Move every existing enum into `enums/<domain>.py`.
- Re-export from old import paths (`obscura/core/types.py` keeps `from obscura.core.enums.agent import AgentEventKind`).
- Resolve the 3-way `ErrorCategory` duplicate; merge into one with prefixed members.
- Convert remaining 38 plain `Enum`s to `StrEnum` where they're compared to strings or serialized.
- **Deliverable**: `git grep "class .*Enum" obscura/` outside `enums/` returns zero hits. All tests pass.

### Phase 2 — Promote `Literal[...]` aliases to enums (≈0.5 day)
- 22 `Literal[...]` aliases → `StrEnum`. Includes `ApprovalStatus`, `BackgroundTaskStatus`, `MemoryEventKind`, `MemorySource`, `PushProvider`, `MCPTransport`, `TemplateMode`, `DiffLineType`, `FilterMode`, `ComparisonOperator`, `ProfileSource`, `AuthProvider`, `MCPLogLevel`, `SideEffects`, `ToolChoiceMode`, `MemoryType`, `WorktreeStatus`, `HealthStatus`, `UIMode`, `OutputMode`, `LogFormat`, `HTTPMethod`.
- **Deliverable**: `rg 'Literal\[' obscura/` only matches genuine wire-format literals (handful).

### Phase 3 — Discriminated unions + Pydantic content blocks (≈2 days)
- `ContentBlock` becomes 4 BaseModel variants (`TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock`) under `Annotated[Union, Field(discriminator="kind")]`.
- Same pattern for A2A `Part` (text/file/data) and A2A task message kinds.
- Same for compiler `SpecKind` (5 specs in `core/compiler/specs.py`).
- Same for `PeerKind` (`AgentRef | RemoteAgentRef | UnixSocketAgentRef`).
- All consumers switch from `block["kind"] == "..."` to `match` / `isinstance`.
- ~80 sites touched.
- **Deliverable**: zero `block["kind"]` reads in `obscura/`.

### Phase 4 — Lifecycle status (≈2 days, the big one)
- All 7 lifecycle enums implement the `Lifecycle` protocol.
- Create `TaskRecord`, `GoalRecord`, `ApprovalRecord`, `WorktreeEntry`, `SessionRecord`, `HealthReport` Pydantic models using `StatusedMixin[…]`.
- Replace SQL string predicates (`status = 'pending'`) with `status = TaskStatus.PENDING.value`.
- Replace dict access (`current["status"]`) with `record.status`.
- ~146 sites touched — biggest single phase.
- **Deliverable**: every status comparison is `record.status == TaskStatus.PENDING`.

### Phase 5 — Action discriminators + ExecutionMode (≈1 day)
- `GoalAction`, `TaskAction`, `BrowserOperation`, `LSPOperation` enums. Already defined in JSON schemas — promote to enums and have schemas reference enum values.
- Unified `ExecutionMode` (run/loop/stream/stream_loop/blocking/aper) replacing 4 scattered Literals.
- Unified `AgentType` (loop/daemon/reactive/scheduled/aper) replacing string `cfg.get("type", "loop")`.
- ~140 sites touched.
- **Deliverable**: tools dispatch on enums. `cfg.get("type", "loop")` becomes `cfg.type` (default in model).

### Phase 6 — Tool execution surface (≈1.5 days)
- `SideEffects` typed on `ToolSpec.side_effects`.
- `ToolChoiceMode` typed on `ToolChoice.mode`.
- `ContentBlockKind` paired with Phase 3.
- Verify `BashRisk`, `MemoryType`, `VerdictKind`, `TriggerKind` used everywhere.
- `BashClassification` Pydantic model for `core/bash_classifier.py`.
- **Deliverable**: every `ToolSpec` field is enum-typed. Tool router dispatch is `dict[ToolName, Tool]`.

### Phase 7 — Internal config dicts → Pydantic (≈2 days)
- `AgentConfig` model replaces dict in `tools/swarm.py`, `agent/supervisor.py`.
- `MCPServerSpec` replaces raw dict reads of `~/.obscura/mcp/core.json`.
- `HookContext` replaces dict passed to hook callbacks (finite key set known).
- `Trigger` discriminated union replaces `daemon_agent` string `trigger.kind`.
- Plugin manifests (`~/.obscura/plugins/builtins/<id>.toml`): wrap with `PluginManifest` model on load.
- **Deliverable**: every `def fn(cfg: dict[str, Any])` in `core/`, `tools/`, `agent/` becomes `def fn(cfg: SomeModel)`. Pyright catches drift.

### Phase 8 — Wire-format boundary models (≈1.5 days)
- `JSONRPCRequest` / `JSONRPCResponse` / `MCPMessage` at MCP server/client boundaries.
- `A2ATaskMessage`, `Task`, `StatusUpdateEvent`, `ArtifactUpdateEvent` at A2A boundary.
- `ToolResult` boundary model — every tool returns the model; `.model_dump()` happens at the egress only.
- HTTP request/response stay header-string-keyed; wrap inputs with `HTTPRequest` / `HTTPResponse` Pydantic models that hold the headers as `Mapping[str, str]`.
- **Deliverable**: `dict[str, Any]` only appears at I/O sinks (network, file, subprocess). Everything internal is typed.

### Phase 9 — Cleanup + enforcement (≈0.5 day)
- Delete the re-export shims from Phase 1.
- Pyright strict expanded across all of `obscura/core/`, `obscura/agent/`, `obscura/tools/`.
- Add ruff rules:
  - Forbid `dict[str, Any]` annotations in `obscura/core/` (custom plugin or `flake8-annotations`).
  - Forbid `==` / `!=` against bare string literals on enum-typed fields (pyright `reportGeneralTypeIssues=error`).
- Update `CLAUDE.md` with the new conventions section.
- **Deliverable**: regression bar set. PRs introducing new loose strings fail CI.

**Total**: ~12 working days, sequenceable, each phase independently mergeable.

---

## 5. Verification Strategy

- **Pyright is the oracle.** Each phase: bump module to strict, fix the errors, ship. The compiler tells you what you missed.
- **Trend audit**: after each phase, `rg '"[a-z_]+"' obscura/<package>/ | rg '== |!= |in \{|in \(' | wc -l` should monotonically decrease.
- **Wire compat**: `StrEnum.value` matches today's strings byte-for-byte. Existing `events.db`, `core.json`, persisted approvals keep working without migration.
- **Snapshot tests**: at every persistence boundary, add a `model_validate` test against a fixture from the prior format to prove parity.
- **Coverage gate**: `pytest --cov=obscura --cov-report=term-missing` already enforces ≥85%; do not let phases regress it.
- **API contract tests**: per-route `model_dump(by_alias=True)` snapshot tests prove HTTP/MCP/A2A consumers see byte-identical output.

---

## 6. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| External callers depend on dict-shape return types (REST, browser ext) | `model_dump(by_alias=True)` produces byte-identical output. Per-route contract test. |
| Import cycles | One-way DAG: `models → enums → mixins`. Pyright's import-cycle check enforces. |
| Mass renames break in-flight branches | Phase 1 keeps re-export shims at old paths. Other branches keep building. Shims deleted only in Phase 9. |
| Pydantic v1 vs v2 drift | Already on v2 (`core/config.py` uses `ConfigDict`). No risk. |
| `frozen=True` breaks code that mutates records | Detect during phase migration; downgrade to non-frozen for that one model. |
| `extra="forbid"` rejects legacy persisted JSON | Boundary models reading from disk use `extra="ignore"`; in-memory models keep `extra="forbid"`. |
| `AgentEvent` Pydantic adds construction overhead in hot loop | Benchmark first in Phase 3. If meaningful, keep dataclass + add `from_payload` validator at the boundary only. |
| Re-export shims rot (never deleted) | Phase 9 explicitly deletes; CI fails the PR if anything still imports from old path. |

---

## 7. Open Questions

1. **`ErrorCategory` merge granularity** — single enum with prefixed members, or split enum + `domain: ErrorDomain` companion? Recommend prefixed members (less code, clearer in stack traces). Decide before Phase 1.
2. **`AgentEvent` Pydantic vs dataclass** — high construction frequency in the agent loop. Benchmark before Phase 3 commits.
3. **Plugin manifests** (`~/.obscura/plugins/builtins/<id>.toml`) — wrap with Pydantic at load time, or read raw? Recommend wrap, since consumers branch on it.
4. **Discriminated unions vs `isinstance`** — pyright handles both; `match` is preferred for new code, but isinstance-chains may be left in legacy callsites if cleaner.

---

## 8. First 4 Tickets (concrete starting points)

1. **Phase 0 wiring**: create `obscura/core/enums/{__init__,_base}.py` + `obscura/core/models/{__init__,_base,_mixins}.py`. Empty packages with `ObscuraModel`, mixins, `Lifecycle` protocol. No behavior changes.
2. **Migrate the easy core 6**: move `Backend`, `Role`, `ChunkKind`, `AgentPhase`, `HookPoint`, `AgentEventKind` from `core/types.py` → `core/enums/agent.py`. Re-export from `core/types.py`. All call sites unchanged.
3. **Resolve `ErrorCategory` 3-way duplicate**: unified `ErrorCategory` in `enums/error.py`. Touches `core/agent_loop.py`, `core/kairos/errors.py`, `core/supervisor/errors.py`.
4. **Pilot the lifecycle pattern**: `ApprovalStatus` Literal → `StrEnum` + `ApprovalRecord` Pydantic model with `IdentifiedMixin + TimestampedMixin + StatusedMixin[ApprovalStatus]`. Smallest standalone slice — validates the pattern end-to-end before scaling to Phase 4's 6 records.

After ticket 4 lands, Phase 4 (lifecycle) is the same template ×6.

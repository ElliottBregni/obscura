# Round 4 — Typed Cores Cleanup

Follow-up to [strings-to-enums-and-pydantic.md](strings-to-enums-and-pydantic.md). Rounds 1–3 landed enums + Pydantic models + shim deletion. What's left:

1. **Pyright debt (87 errors)** — concentrated in 4 files; all user-territory in-flight code.
2. **`dict[str, Any]` migration (234 sites in `obscura/core/`)** — gate to enabling ANN401 in CI.
3. **ANN401 enforcement** — landed only after (2) is complete.

Total est. ~3 working days of agent time, splittable into parallel teams.

---

## 1. Goals

- Pyright clean (`0 errors`) on `obscura/core/` and on every tracked production module.
- Every internal-shape `dict[str, Any]` in `obscura/core/` replaced with a typed `BaseModel` subclass (or `dict[<KeyEnum>, <ValueModel>]`, or an explicit `Mapping[str, Any]` *only* at I/O boundaries).
- ANN401 enabled in `[tool.ruff.lint.per-file-ignores]` so PRs introducing new untyped `Any` annotations in `obscura/core/` fail CI.

## 2. Non-goals

- `dict[str, Any]` in non-`core/` packages (tools/, plugins/, integrations/) — out of scope unless they cross a `core/` boundary.
- Test-side type annotations.
- Backward-compat shims (already deleted in Round 3).

---

## 3. Workstream A — Pyright debt (87 errors)

### 3.1 Where they live

| File | Errors | Owner |
|---|---|---|
| `eval/engine.py` | 32 | user's v1-removal sweep — needs `make_agent_loop` migration |
| `core/agent_loop_v2.py` | 28 | mid-stream chunk parsing, `_TurnDAGContext` forward-ref, sort/iter on `TurnDAG` |
| `integrations/messaging/runners.py` | 8 | downstream of agent_loop_v2 type changes |
| `core/client/__init__.py` | 4 | needs Backend/Role import path tidy |
| `integrations/messaging/kairos_runner.py` | 3 | same as runners.py |
| `core/backend_retry.py` | 3 | `RetryingBackend` doesn't fully implement `BackendProtocol` |
| `cli/kairos_commands.py` | 3 | AgentLoop v1 reference (likely already fixed in user's parallel work) |
| `core/stream.py` | 1 | small |

### 3.2 Approach

Three parallel teams (file-disjoint):

- **Team Engine** — `eval/engine.py`. Migrate v1 `AgentLoop(...)` constructions to `make_agent_loop(...)`. Verify async-iter shapes match.
- **Team v2** — `core/agent_loop_v2.py` + `core/agent_loop_predictive.py` + `core/backend_retry.py` + `core/stream.py`. The biggest chunk. Fix `_TurnDAGContext` forward ref, type the chunk parser, make `RetryingBackend` proxy the missing `BackendProtocol` methods (`stop`, `send`, `create_session`, `resume_session`, `list_sessions`, `delete_session`, `register_tool`) via explicit delegation.
- **Team Messaging+CLI** — `integrations/messaging/runners.py` + `integrations/messaging/kairos_runner.py` + `cli/kairos_commands.py` + `core/client/__init__.py`. Smaller, related changes that ripple from v2.

Each team verifies pyright clean on owned files before commit.

---

## 4. Workstream B — `dict[str, Any]` migration (234 sites in `core/`)

### 4.1 Hot spots

| File | Sites | Likely shape |
|---|---|---|
| `core/eval_checks.py` | 14 | eval criteria configs — make a `CheckConfig` Pydantic model |
| `core/config_io.py` | 13 | TOML/JSON loaders — `BoundaryModel` per file shape |
| `core/types.py` | 10 | mostly already-typed surfaces with `Any` for tool args (legitimate — keep) |
| `core/models/lifecycle.py` | 10 | mostly `Mapping[str, Any]` for free-form metadata (keep — it's the metadata field) |
| `core/models/specs.py` | 9 | spec body — domain-specific |
| `core/event_store.py` | 9 | SQLite row dicts — wrap with record models from Round 2 |
| `core/dag.py` | 9 | DAG node payloads |
| `core/models/tool_result.py` | 8 | `data: Any` is correct (tool results are heterogeneous JSON) — keep |
| `core/kairos/types.py` | 8 | dataclass-shaped, easy migration |
| `core/agent_loop_predictive.py` | 7 | predictive-cache state |
| `core/workspace.py` | 6 | workspace config |
| `core/supervisor/agent_templates.py` | 6 | template body |
| `core/postgres_event_store.py` | 6 | SQL row dicts — same fix as event_store.py |
| `core/parallel_plan.py` | 6 | parallel plan state |
| `core/migrate_external.py` | 6 | migration-tool state |
| (rest) | ~107 | scattered |

### 4.2 Triage rules

For each `dict[str, Any]` usage:

| Pattern | Resolution |
|---|---|
| `metadata: Mapping[str, Any]` field on a record | **Keep** — that's metadata's intended shape. |
| Tool args / tool results / JSON-RPC `params`/`result` | **Keep** — heterogeneous JSON is genuinely `Any`. |
| Internal config dict passed between functions | **Migrate** — Pydantic `BoundaryModel` (lenient) or `ObscuraModel` (strict). |
| SQLite row dict | **Migrate** — use record models from Round 2 (`from_row()`/`to_row()`). |
| Wire-format dict at I/O entry/exit | **Wrap with `BoundaryModel.model_validate(raw)`** at the seam. |
| Free-form sub-dispatch (e.g. plugin hook payloads) | **Migrate** — discriminated union if there's a `kind` field. |

### 4.3 Approach

Four parallel teams, each owning a slice (~50–60 sites):

- **Team Eval** — `core/eval_checks.py`, `core/dag.py`, `core/parallel_plan.py`, `core/agent_loop_predictive.py`
- **Team Storage** — `core/config_io.py`, `core/event_store.py`, `core/postgres_event_store.py`, `core/workspace.py`, `core/migrate_external.py`
- **Team Subsystems** — `core/kairos/*`, `core/supervisor/*`
- **Team Models+Misc** — `core/models/*` (audit existing sites — most are legitimate), `core/types.py`, remaining scattered files

Each team produces ~3–8 new `BoundaryModel`/`ObscuraModel` subclasses per slice, in `obscura/core/models/<domain>.py`. Reuses Round 1 enums where applicable.

### 4.4 Risk

These dict shapes are often reflected to disk (`events.db`, plugin manifests, config files). Wire-format compatibility must be preserved:

- Pydantic models for persisted shapes use `BoundaryModel` (`extra="ignore"`).
- `model_dump(by_alias=True)` is the only path for serialization.
- Snapshot tests: capture the JSON shape *before* migration, assert byte-for-byte parity *after*.

---

## 5. Workstream C — ANN401 enforcement

After (B) lands, `obscura/core/` should have only legitimate `Any` usage (metadata fields, tool args, wire-format payloads). Enable ANN401:

```toml
# pyproject.toml
[tool.ruff.lint]
extend-select = ["ANN401"]

[tool.ruff.lint.per-file-ignores]
# Tests, scripts, and non-core packages legitimately use Any.
"tests/**/*.py" = ["ANN401"]
"scripts/**/*.py" = ["ANN401"]
"obscura/!(core)/**/*.py" = ["ANN401"]  # everything outside core
```

Audit remaining ANN401 hits in `obscura/core/`:
- If legitimate (metadata, tool args, JSON-RPC), add per-file `# noqa: ANN401` with a one-line reason.
- If migratable but missed, fix.

CI gate: `make lint` fails on new `dict[str, Any]` in `obscura/core/`.

---

## 6. Phasing

| Phase | Workstream | Time | Parallelism |
|---|---|---|---|
| 4.A | Pyright debt | 1d | 3 teams parallel |
| 4.B | `dict[str, Any]` migration | 1.5d | 4 teams parallel |
| 4.C | ANN401 enforcement | 0.5d | 1 team sequential |

Each phase ends green: `make lint && make typecheck && pytest -m "not e2e"`.

Phase 4.A runs first (resolves blocking errors that hide other issues during 4.B). 4.B and 4.C must be sequential.

---

## 7. Verification

- **Pyright trend**: 87 → 0 by end of 4.A.
- **`Any` count in `core/`**: 757 → ~50 (legitimate residual) by end of 4.B.
- **CI gate**: ANN401 active by end of 4.C; new PRs fail on untyped `Any` in core.
- **Wire compat**: snapshot tests on `events.db`, plugin manifests, MCP/A2A payloads — byte-identical pre/post.

---

## 8. Stretch (deferred)

- Migrate `dict[str, Any]` in `obscura/tools/`, `obscura/plugins/`, `obscura/integrations/` (~907 sites). Round 5 territory.
- Add ruff rule banning string-literal `==`/`!=` against StrEnum-typed fields (custom plugin or pyright `reportGeneralTypeIssues=error` wrap).
- Delete the legacy `Pydantic model alias` re-exports in `integrations/a2a/types.py` (`Task = A2ATask`, `Part = A2APart`) — Team C explicitly left those for a later round since they're model re-exports (not enum re-exports) and have wider caller surface.

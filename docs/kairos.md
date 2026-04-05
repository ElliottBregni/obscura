# Kairos — Autonomous Goal Runtime

Kairos is Obscura's autonomous background runtime for long-horizon goals. You
describe an outcome; Kairos decomposes it into a Plan of Tasks, executes them
with an agent loop, checkpoints progress, and escalates to you only when it
cannot proceed autonomously.

---

## Concepts

| Concept | Description |
|---|---|
| **Goal** | A user-defined outcome with a title, description, optional success criteria, and a budget. The unit of work you hand to Kairos. |
| **Plan** | A revisable ordered sequence of Tasks for a Goal. When the agent decides the plan is wrong it creates a new revision (old plan is `superseded`). |
| **Task** | An atomic executable step inside a Plan. Carries a description, optional tool hint, dependency list, and retry limit. |
| **TaskResult** | The outcome of a completed Task: summary, raw output, error string, turns used, tokens used, elapsed ms. |
| **Checkpoint** | A durable progress snapshot written after every N tasks (default 3), on plan revision, or on intervention. Contains completed/pending task lists, a progress summary, and current budget usage. Survives restarts. |
| **Intervention** | A point where the agent cannot proceed autonomously. Blocks the Goal until you respond via CLI or API. |
| **Budget** | Per-goal execution limits. `0` means unlimited on every dimension. |
| **GoalRunContext** | Immutable snapshot of a goal's context for a single execution tick (used internally). |

### GoalStatus states

```
PENDING   Created, not yet started
PLANNING  Decomposing the goal into a Plan
ACTIVE    Plan exists and is being executed
PAUSED    Execution suspended (user-requested or system)
BLOCKED   Waiting on an unresolved Intervention
COMPLETED All success criteria met
FAILED    Unrecoverable failure
CANCELLED User-cancelled (terminal)
```

### TaskStatus states

```
PENDING           Not yet started
RUNNING           Actively executing
SUCCEEDED         Finished successfully
FAILED            Failed (retries exhausted)
RETRYING          Retrying after failure
BLOCKED           Waiting on an Intervention
APPROVAL_REQUIRED Needs explicit human approval before proceeding
SKIPPED           Skipped (dependency failed or plan revision)
```

### InterventionKind values

| Kind | When raised |
|---|---|
| `AMBIGUITY` | Goal or task is unclear |
| `RISK` | Action is irreversible or high-risk |
| `AUTHORIZATION` | Required permission exceeds current scope |
| `BUDGET_EXCEEDED` | A budget dimension was exceeded |
| `APPROVAL` | Agent explicitly requests approval |
| `CLARIFICATION` | Agent needs more information to proceed |

---

## Goal lifecycle

```
                    ┌─────────┐
                    │ PENDING │
                    └────┬────┘
                         │ start
                    ┌────▼─────┐
                    │ PLANNING │
                    └────┬─────┘
               success   │   failure/cancel
            ┌────────────┘
       ┌────▼────┐
       │ ACTIVE  │◄──────────────┐
       └─┬──┬───┘               │
         │  │                   │ resume
   pause │  │ intervention   ┌──┴──────┐
         │  │                │  PAUSED │
    ┌────▼─┐│                └─────────┘
    │PAUSED│┘
    └──────┘
         │
         │ intervention raised
    ┌────▼───┐
    │ BLOCKED│
    └────┬───┘
         │ intervention resolved
         └──────────► ACTIVE
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
          COMPLETED   FAILED   CANCELLED
```

Valid transitions (from `VALID_GOAL_TRANSITIONS` in `types.py`):

| From | To |
|---|---|
| `PENDING` | `PLANNING`, `CANCELLED` |
| `PLANNING` | `ACTIVE`, `FAILED`, `CANCELLED` |
| `ACTIVE` | `PAUSED`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `PAUSED` | `ACTIVE`, `CANCELLED` |
| `BLOCKED` | `ACTIVE`, `FAILED`, `CANCELLED` |
| `COMPLETED` | _(terminal)_ |
| `FAILED` | _(terminal)_ |
| `CANCELLED` | _(terminal)_ |

---

## Quick start

### CLI

```bash
# Create and run a goal immediately
obscura kairos run "Audit the auth module for security issues" \
  --description "Check for common vulnerabilities, outdated deps, missing tests" \
  --criteria "No high-severity findings" \
  --criteria "Test coverage >= 80%" \
  --budget-turns 40 \
  --budget-seconds 300

# Create without running (inspect first)
obscura kairos run "Refactor the data layer" --dry-run
```

### API

```bash
curl -X POST http://localhost:7373/api/v1/goals \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Audit the auth module for security issues",
    "description": "Check for common vulnerabilities, outdated deps, missing tests",
    "success_criteria": ["No high-severity findings", "Test coverage >= 80%"],
    "budget": {
      "max_turns": 40,
      "max_wall_seconds": 300
    }
  }'
```

Response (`201 Created`):

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Audit the auth module for security issues",
  "status": "pending",
  "created_at": "2026-04-04T10:00:00Z",
  "budget": {
    "max_tasks": 0,
    "max_turns": 40,
    "max_wall_seconds": 300.0,
    "max_tokens": 0
  }
}
```

### Python SDK

```python
from obscura.core.kairos import Kairos, KairosConfig
from obscura.core.kairos.types import GoalBudget

kairos = Kairos(db_path="~/.obscura/kairos.db", agent_loop=loop)

goal_id = await kairos.create_goal(
    title="Refactor the auth module",
    description="...",
    success_criteria=["All tests pass", "No mypy errors"],
    budget=GoalBudget(max_turns=50, max_wall_seconds=600),
)

async for event in kairos.run(goal_id):
    print(event.kind, event.payload)
```

---

## CLI reference

All commands are under the `obscura kairos` group.

### `run`

Create and execute a goal.

```
obscura kairos run TITLE [OPTIONS]

Options:
  -d, --description TEXT      Goal description
  -c, --criteria TEXT         Success criterion (repeatable)
  --budget-turns  INTEGER     Max model turns across all tasks  [default: 0]
  --budget-tasks  INTEGER     Max tasks  [default: 0]
  --budget-seconds FLOAT      Max wall-clock seconds  [default: 0.0]
  --dry-run                   Create goal but do not execute
```

### `status`

Show goal status. Defaults to active goals only.

```
obscura kairos status [OPTIONS]

Options:
  -g, --goal-id TEXT   Show a specific goal (verbose)
  --all                Show all goals (not just active)
```

### `pause`

Suspend a running goal. Resumes from last checkpoint.

```
obscura kairos pause GOAL_ID
```

### `resume`

Resume a paused goal. Streams events until completion.

```
obscura kairos resume GOAL_ID
```

### `cancel`

Cancel a goal permanently (terminal — cannot be undone). Prompts for confirmation.

```
obscura kairos cancel GOAL_ID
```

### `respond`

Resolve a pending intervention to unblock a goal.

```
obscura kairos respond GOAL_ID INTERVENTION_ID RESPONSE
```

Example:

```bash
obscura kairos respond 550e8400 intv-abc123 "Yes, proceed with the migration"
```

### `goals`

List goals with optional status filter and result limit.

```
obscura kairos goals [OPTIONS]

Options:
  -s, --status TEXT   Filter: pending / active / paused / completed / failed / cancelled
  -n, --limit INTEGER Max results  [default: 20]
```

---

## API reference

Base prefix: `/api/v1`

### `GET /goals`

List goals. Optionally filter by status.

Query params: `status`, `limit` (default 50), `offset` (default 0).

```bash
curl "http://localhost:7373/api/v1/goals?status=active&limit=10"
```

```json
{
  "goals": [ { "id": "...", "title": "...", "status": "active", ... } ],
  "total": 1,
  "limit": 10,
  "offset": 0
}
```

### `POST /goals`

Create a new goal. Returns `201`.

```json
{
  "title": "string (required)",
  "description": "string",
  "priority": 50,
  "success_criteria": ["string"],
  "tags": ["string"],
  "budget": {
    "max_tasks": 0,
    "max_turns": 0,
    "max_wall_seconds": 0.0,
    "max_tokens": 0
  },
  "metadata": {}
}
```

### `GET /goals/{goal_id}`

Get a single goal by ID. Returns `404` if not found.

### `POST /goals/{goal_id}/pause`

Pause a running goal. Returns `409` if the transition is invalid.

### `POST /goals/{goal_id}/resume`

Resume a paused goal. Returns `409` if the transition is invalid.

### `POST /goals/{goal_id}/cancel`

Cancel a goal. Terminal — cannot be undone. Returns `409` if already in a
terminal state.

### `GET /goals/{goal_id}/tasks`

List all tasks for a goal.

```json
{
  "goal_id": "...",
  "tasks": [
    {
      "id": "...",
      "title": "...",
      "description": "...",
      "status": "succeeded",
      "sequence": 1,
      "depends_on": [],
      "created_at": "2026-04-04T10:01:00Z"
    }
  ]
}
```

---

## Budget limits

`GoalBudget` is a frozen dataclass. Every field defaults to `0` (unlimited).

| Field | Type | Default | Description |
|---|---|---|---|
| `max_tasks` | `int` | `0` | Max tasks Kairos will execute for this goal |
| `max_turns` | `int` | `0` | Max total model turns across all tasks |
| `max_wall_seconds` | `float` | `0.0` | Wall-clock deadline in seconds |
| `max_tokens` | `int` | `0` | Approximate token budget (input + output) |
| `max_retries_per_task` | `int` | `3` | Max retries before a task is marked failed |

When any limit is hit, Kairos raises a `BUDGET_EXCEEDED` intervention. The goal
moves to `BLOCKED`. You can respond to the intervention to extend the budget or
cancel.

---

## Configuration

### KairosConfig fields

`KairosConfig` is a frozen dataclass in `obscura.core.kairos.types`.

| Field | Default | Description |
|---|---|---|
| `max_plan_tasks` | `20` | Max tasks allowed in a single plan |
| `max_plan_revisions` | `5` | How many times a plan can be revised before failing |
| `default_model` | `"copilot"` | Model used for planning and task execution |
| `task_timeout_seconds` | `300.0` | Per-task wall-clock timeout |
| `planning_timeout_seconds` | `60.0` | Timeout for the planning phase |
| `checkpoint_every_n_tasks` | `3` | Auto-checkpoint interval (tasks completed) |
| `persist_checkpoints` | `True` | Write checkpoints to `kairos.db` |
| `auto_pause_on_risk` | `True` | Pause goal immediately when a RISK intervention is raised |
| `max_pending_interventions` | `5` | Max unresolved interventions before goal is failed |
| `heartbeat_interval` | `10.0` | Seconds between heartbeat events |
| `default_budget` | `GoalBudget()` | Budget applied to goals without an explicit budget |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OBSCURA_KAIROS` | `1` | Set to `false` or `0` to disable the Kairos daemon entirely |
| `OBSCURA_KAIROS_PROACTIVE` | `1` | Set to `false` to disable proactive tick-based actions (saves tokens) |
| `OBSCURA_KAIROS_DREAM` | `1` | Set to `false` to disable dream consolidation (saves tokens) |
| `OBSCURA_KAIROS_VAULT_SYNC` | `1` | Set to `false` to disable Obsidian vault sync |

The daemon can also be toggled at runtime with the `/kairos on` and `/kairos off`
chat commands.

---

## Interventions

Kairos raises an intervention when it cannot proceed without human input. The
goal moves to `BLOCKED` and all task execution stops.

When an intervention is raised you will see:

```
⚠ Intervention Raised  id=intv-abc123…
```

Check the intervention details:

```bash
obscura kairos status --goal-id 550e8400
```

Respond to unblock the goal:

```bash
obscura kairos respond 550e8400 intv-abc123 "Proceed — I accept the risk"
```

After a successful response the goal transitions back to `ACTIVE` and execution
resumes from the last checkpoint.

If `auto_pause_on_risk` is enabled (default), `RISK` interventions also set the
goal to `PAUSED` before `BLOCKED`, giving you time to review before the agent
does anything irreversible.

---

## Web UI

The Goals page is available at `/goals` in the Obscura web UI.

**Filter tabs** — All | Active | Completed | Failed

- "Active" includes goals with status `active`, `pending`, or `paused`.
- "Failed" includes goals with status `failed` or `cancelled`.

**Create form** — title, description, success criteria (line-separated), and a
max-turns budget field. Submits to `POST /api/goals` and refreshes the list
every 8 seconds.

**Goal card actions** — Pause, Resume, and Cancel buttons call the corresponding
REST endpoints directly. Status icons update in real time.

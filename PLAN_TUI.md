# Obscura TUI — Implementation Plan

## Overview

Add a full-featured Terminal User Interface to Obscura as `obscura-sdk tui`. Inspired by Claude Code's interactive experience — streaming responses, inline diffs, mode switching, tool use visualization, and session persistence. Built on Textual + Rich, reusing ObscuraClient and existing stream adapters directly (no server required).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Obscura TUI (Textual App)                                   │
│                                                              │
│  ┌─────────────┐  ┌──────────────────────────────────────┐  │
│  │  Sidebar     │  │  Main Panel                          │  │
│  │             │  │                                      │  │
│  │  Mode:      │  │  ┌──────────────────────────────┐   │  │
│  │  ● Ask      │  │  │  Message History              │   │  │
│  │  ○ Plan     │  │  │  (scrollable, markdown+code)  │   │  │
│  │  ○ Code     │  │  │                              │   │  │
│  │  ○ Diff     │  │  │  User: "review this file"    │   │  │
│  │             │  │  │  Agent: streaming response... │   │  │
│  │  Backend:   │  │  │  [Tool: read_file] ✓         │   │  │
│  │  claude     │  │  │  [Thinking...] collapsed      │   │  │
│  │             │  │  └──────────────────────────────┘   │  │
│  │  Session:   │  │                                      │  │
│  │  abc123     │  │  ┌──────────────────────────────┐   │  │
│  │             │  │  │  Input Area                    │   │  │
│  │  Memory:    │  │  │  > type here...               │   │  │
│  │  3 keys     │  │  │                              │   │  │
│  │             │  │  └──────────────────────────────┘   │  │
│  └─────────────┘  └──────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ Status Bar: [ASK] claude-sonnet | Session: abc1 | ⏱ 2.3s││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

---

## Modes

### 1. Ask Mode (default)
- Free-form Q&A with streaming responses
- Markdown rendering for responses (headers, lists, bold, etc.)
- Syntax-highlighted code blocks
- Thinking blocks shown collapsed by default (expand with `t`)
- Tool use shown inline with status indicators (⏳ → ✓ / ✗)

### 2. Plan Mode
- Send a task description, agent responds with a structured plan
- Plan steps shown as a numbered checklist
- User can approve (y), reject (n), or edit steps
- Approved plan gets stored in session memory
- On approval, optionally auto-transitions to Code mode to execute

### 3. Code Mode
- Agent can read/write/edit files via tool use
- File changes shown as inline diffs (before → after)
- Each file change requires explicit accept/reject
- Accepted changes written to disk
- Rejected changes discarded with feedback to agent
- Shows file tree of modified files in sidebar

### 4. Diff Mode
- Review all pending/accepted changes from Code mode
- Side-by-side or unified diff view (toggle with `d`)
- Navigate between hunks with `j`/`k`
- Accept/reject individual hunks or entire files
- Commit integration: generate commit message, stage, commit

---

## File Structure

```
sdk/tui/
├── __init__.py              # Package init, version
├── app.py                   # ObscuraTUI(App) — main Textual application
├── modes.py                 # TUIMode enum, ModeManager state machine
├── session.py               # TUISession — conversation history, persistence
├── backend_bridge.py        # AsyncIO bridge between Textual and ObscuraClient
├── widgets/
│   ├── __init__.py
│   ├── message_list.py      # MessageList — scrollable conversation view
│   ├── message_bubble.py    # MessageBubble — single user/assistant message
│   ├── input_area.py        # PromptInput — multi-line input with keybindings
│   ├── sidebar.py           # Sidebar — mode selector, backend info, session info
│   ├── status_bar.py        # StatusBar — mode, model, timing, token count
│   ├── thinking_block.py    # ThinkingBlock — collapsible thinking content
│   ├── tool_status.py       # ToolStatus — tool use with spinner/result
│   ├── diff_view.py         # DiffView — unified/side-by-side diff display
│   ├── plan_view.py         # PlanView — numbered plan with approve/reject
│   └── file_tree.py         # FileTree — modified files tree in sidebar
├── diff_engine.py           # Diff computation, hunk parsing, patch apply
├── file_ops.py              # Safe file read/write/backup for Code mode
└── themes.py                # Dark/light theme definitions (CSS)
```

---

## Key Classes & Interfaces

### `app.py` — ObscuraTUI

```python
class ObscuraTUI(App):
    """Main Textual application."""

    BINDINGS = [
        ("ctrl+a", "switch_mode('ask')", "Ask"),
        ("ctrl+p", "switch_mode('plan')", "Plan"),
        ("ctrl+e", "switch_mode('code')", "Code"),
        ("ctrl+d", "switch_mode('diff')", "Diff"),
        ("ctrl+b", "toggle_sidebar", "Sidebar"),
        ("ctrl+n", "new_session", "New Session"),
        ("ctrl+q", "quit", "Quit"),
        ("escape", "cancel_stream", "Cancel"),
    ]

    def __init__(self, backend: str, model: str | None, cwd: str | None):
        ...

    def compose(self) -> ComposeResult:
        yield Sidebar()
        yield MessageList()
        yield PromptInput()
        yield StatusBar()

    async def on_prompt_submitted(self, event: PromptInput.Submitted) -> None:
        """Handle user input — route to current mode handler."""

    async def action_switch_mode(self, mode: str) -> None:
        """Switch between ask/plan/code/diff modes."""

    async def action_cancel_stream(self) -> None:
        """Cancel the current streaming response."""
```

### `modes.py` — Mode System

```python
class TUIMode(Enum):
    ASK = "ask"
    PLAN = "plan"
    CODE = "code"
    DIFF = "diff"

class ModeManager:
    """State machine for mode transitions."""

    current: TUIMode
    pending_changes: list[FileChange]  # Files modified in Code mode
    active_plan: Plan | None           # Current plan in Plan mode

    def switch(self, mode: TUIMode) -> None: ...
    def get_system_prompt(self) -> str: ...  # Mode-specific system prompt
```

### `backend_bridge.py` — Async Bridge

```python
class BackendBridge:
    """Manages ObscuraClient lifecycle and streams chunks to TUI widgets."""

    def __init__(self, backend: str, model: str | None, cwd: str | None):
        self._client: ObscuraClient | None = None

    async def connect(self) -> None:
        """Initialize ObscuraClient."""

    async def stream_prompt(
        self,
        prompt: str,
        on_text: Callable[[str], None],
        on_thinking: Callable[[str], None],
        on_tool_start: Callable[[str], None],
        on_tool_result: Callable[[str], None],
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Stream a prompt through ObscuraClient, dispatching chunks to callbacks.

        Uses existing ChunkKind routing:
        - TEXT_DELTA → on_text
        - THINKING_DELTA → on_thinking
        - TOOL_USE_START → on_tool_start
        - TOOL_RESULT → on_tool_result
        - DONE → on_done
        - ERROR → on_error
        """

    async def send_prompt(self, prompt: str) -> Message:
        """Non-streaming send."""

    async def disconnect(self) -> None: ...
```

### `session.py` — Session Persistence

```python
@dataclass
class ConversationTurn:
    role: str           # "user" or "assistant"
    content: str
    timestamp: datetime
    mode: TUIMode
    metadata: dict      # tool_uses, thinking, timing, etc.

class TUISession:
    """Manages conversation history and persists to disk."""

    session_id: str
    turns: list[ConversationTurn]
    file_path: Path     # ~/.obscura/tui_sessions/<id>.json

    def add_turn(self, turn: ConversationTurn) -> None: ...
    def save(self) -> None: ...

    @classmethod
    def load(cls, session_id: str) -> TUISession: ...

    @classmethod
    def list_sessions(cls) -> list[dict]: ...
```

### `diff_engine.py` — Diff System

```python
@dataclass
class FileChange:
    path: Path
    original: str          # Original file content
    modified: str          # New content
    hunks: list[DiffHunk]
    status: str            # "pending" | "accepted" | "rejected"

@dataclass
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine]  # Each line tagged +/-/space

class DiffEngine:
    """Compute and apply diffs."""

    def compute(self, original: str, modified: str) -> list[DiffHunk]: ...
    def apply_hunks(self, original: str, accepted: list[DiffHunk]) -> str: ...
    def format_unified(self, change: FileChange) -> str: ...
    def format_side_by_side(self, change: FileChange, width: int) -> str: ...
```

### `file_ops.py` — File Operations

```python
class FileOps:
    """Safe file operations with backup."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.backup_dir = cwd / ".obscura_backups"

    def read(self, path: str) -> str: ...
    def write(self, path: str, content: str) -> FileChange: ...
    def backup(self, path: str) -> Path: ...
    def restore(self, path: str) -> None: ...
```

---

## Key Widgets

### `message_bubble.py`
- Rich Markdown rendering for assistant responses
- Syntax highlighting via Rich for code blocks
- Collapsible thinking blocks (ThinkingBlock widget)
- Inline tool use indicators (ToolStatus widget)
- User messages styled differently (right-aligned or colored)

### `diff_view.py`
- Unified diff with green/red line coloring
- Hunk-level navigation (j/k keys)
- Per-hunk accept/reject (a/r keys)
- File-level accept all / reject all (A/R keys)
- Line numbers in gutter

### `plan_view.py`
- Numbered step list with checkboxes
- Each step: approve ✓ / reject ✗ / edit ✏
- Summary bar: "3/5 steps approved"
- "Execute Plan" button when all steps decided
- Stores approved plan in memory for Code mode context

### `input_area.py`
- Multi-line text input (Shift+Enter for newline, Enter to submit)
- Mode indicator prefix: `[ASK]>`, `[PLAN]>`, `[CODE]>`, `[DIFF]>`
- Slash commands: `/mode ask`, `/backend claude`, `/session list`, `/clear`, `/help`
- Command history (up/down arrows)
- File path autocomplete in Code mode (Tab)

### `sidebar.py`
- Current mode selector (radio buttons)
- Backend + model display
- Session info (ID, turn count, duration)
- In Code mode: file tree of changed files
- In Diff mode: file list with status icons (✓ accepted, ✗ rejected, ● pending)
- Memory namespace browser (expandable)

---

## Slash Commands

| Command | Description |
|---------|-------------|
| `/mode <ask\|plan\|code\|diff>` | Switch mode |
| `/backend <claude\|copilot>` | Switch backend |
| `/model <model-id>` | Change model |
| `/session new` | Start new session |
| `/session list` | List saved sessions |
| `/session load <id>` | Resume session |
| `/clear` | Clear conversation |
| `/memory list` | Show memory keys |
| `/memory get <key>` | Read memory value |
| `/diff show` | Show all pending diffs |
| `/diff accept-all` | Accept all changes |
| `/diff reject-all` | Reject all changes |
| `/help` | Show help |
| `/quit` | Exit TUI |

---

## Keybindings

| Key | Action |
|-----|--------|
| `Enter` | Submit prompt |
| `Shift+Enter` | New line in input |
| `Ctrl+A` | Switch to Ask mode |
| `Ctrl+P` | Switch to Plan mode |
| `Ctrl+E` | Switch to Code mode |
| `Ctrl+D` | Switch to Diff mode |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+N` | New session |
| `Ctrl+C` | Cancel current stream |
| `Ctrl+Q` | Quit |
| `Escape` | Cancel / close modal |
| `t` | Toggle thinking block (in message view) |
| `j/k` | Navigate hunks (in diff view) |
| `a` | Accept hunk (in diff view) |
| `r` | Reject hunk (in diff view) |
| `A` | Accept all hunks in file |
| `R` | Reject all hunks in file |
| `d` | Toggle unified/side-by-side diff |
| `Up/Down` | Command history |
| `Tab` | Autocomplete (Code mode) |

---

## CLI Entry Point

Add `tui` subcommand to `sdk/cli.py`:

```python
# In build_parser()
tui_parser = sub.add_parser("tui", help="Launch interactive TUI")
tui_parser.add_argument("--backend", default="claude", choices=["claude", "copilot"])
tui_parser.add_argument("--model", default=None)
tui_parser.add_argument("--cwd", default=".")
tui_parser.add_argument("--session", default=None, help="Resume session ID")
tui_parser.add_argument("--mode", default="ask", choices=["ask", "plan", "code", "diff"])

# In main()
if args.command == "tui":
    return _run_tui(args)
```

Usage:
```bash
obscura-sdk tui                              # Default: claude, ask mode
obscura-sdk tui --backend copilot            # Use Copilot
obscura-sdk tui --mode code --cwd ./myproject # Start in code mode
obscura-sdk tui --session abc123             # Resume session
```

---

## Stream Chunk → Widget Mapping

```
ChunkKind.TEXT_DELTA      → MessageBubble.append_text(chunk.text)
ChunkKind.THINKING_DELTA  → ThinkingBlock.append(chunk.text)
ChunkKind.TOOL_USE_START   → ToolStatus.start(chunk.tool_name)
ChunkKind.TOOL_USE_DELTA   → ToolStatus.update(chunk.tool_input_delta)
ChunkKind.TOOL_RESULT      → ToolStatus.complete(chunk.text)
ChunkKind.DONE             → MessageBubble.finalize(), StatusBar.update_timing()
ChunkKind.ERROR            → MessageBubble.show_error(chunk.text)
```

---

## Mode-Specific System Prompts

Each mode injects a system prompt prefix to guide agent behavior:

- **Ask**: Default system prompt (general Q&A)
- **Plan**: "You are in planning mode. Respond with structured, numbered implementation plans. Each step should be actionable and specific. Do not write code yet."
- **Code**: "You are in code mode. Use tools to read and write files. Show your changes clearly. Explain each change briefly."
- **Diff**: "You are reviewing code changes. Analyze the diffs provided and give feedback on correctness, style, and potential issues."

---

## Implementation Phases

### Phase 1: Core Shell (3-4 days)
**Goal**: Working TUI that can send/receive messages in Ask mode

Files to create:
- `sdk/tui/__init__.py`
- `sdk/tui/app.py` — App scaffold with compose(), basic layout
- `sdk/tui/backend_bridge.py` — ObscuraClient wrapper with stream routing
- `sdk/tui/session.py` — In-memory conversation history (persistence later)
- `sdk/tui/modes.py` — TUIMode enum, basic ModeManager
- `sdk/tui/themes.py` — Dark theme CSS
- `sdk/tui/widgets/__init__.py`
- `sdk/tui/widgets/message_list.py` — Scrollable message container
- `sdk/tui/widgets/message_bubble.py` — Rich markdown rendering
- `sdk/tui/widgets/input_area.py` — Basic prompt input (Enter to submit)
- `sdk/tui/widgets/status_bar.py` — Mode + model display

Modify:
- `sdk/cli.py` — Add `tui` subcommand

Tests:
- `tests/test_tui_modes.py` — Mode switching logic
- `tests/test_tui_session.py` — Conversation history

**Deliverable**: `obscura-sdk tui` launches, connects to Claude/Copilot, streams responses with markdown rendering.

### Phase 2: Plan Mode + Thinking Blocks (2-3 days)
**Goal**: Plan mode with approve/reject, thinking block visualization

Files to create:
- `sdk/tui/widgets/thinking_block.py` — Collapsible thinking content
- `sdk/tui/widgets/tool_status.py` — Tool use indicator with status
- `sdk/tui/widgets/plan_view.py` — Plan step list with approval UI
- `sdk/tui/widgets/sidebar.py` — Mode selector, session info

Tests:
- `tests/test_tui_plan.py` — Plan parsing, approval flow

**Deliverable**: Thinking blocks collapse/expand, tool use shows inline, Plan mode renders structured plans with approve/reject per step.

### Phase 3: Code Mode + Diff Engine (3-4 days)
**Goal**: File read/write with inline diffs, accept/reject changes

Files to create:
- `sdk/tui/diff_engine.py` — Diff computation using difflib
- `sdk/tui/file_ops.py` — Safe read/write with backup
- `sdk/tui/widgets/diff_view.py` — Unified/side-by-side diff display
- `sdk/tui/widgets/file_tree.py` — Changed files tree

Tests:
- `tests/test_tui_diff.py` — Diff computation, hunk accept/reject
- `tests/test_tui_file_ops.py` — File backup/restore

**Deliverable**: Code mode shows file changes as diffs, user can accept/reject per hunk, accepted changes write to disk with backup.

### Phase 4: Diff Mode + Session Persistence (2-3 days)
**Goal**: Dedicated diff review mode, session save/load

Updates:
- `sdk/tui/session.py` — Add JSON persistence to `~/.obscura/tui_sessions/`
- `sdk/tui/widgets/diff_view.py` — Full hunk navigation, side-by-side toggle
- `sdk/tui/widgets/sidebar.py` — File status in diff mode

Add slash commands to `input_area.py`

Tests:
- `tests/test_tui_session_persist.py` — Save/load/list sessions
- `tests/test_tui_slash_commands.py` — Command parsing

**Deliverable**: Full diff review workflow, sessions persist across restarts, slash commands functional.

### Phase 5: Polish + Tests (2-3 days)
**Goal**: Edge cases, error handling, test coverage >80%

- Error recovery (backend disconnect, timeout)
- Graceful degradation (no backend → offline mode with session review)
- Input history (up/down arrows)
- File path autocomplete
- Memory browser in sidebar
- Responsive layout (small terminal handling)
- Integration tests with mock backend
- Screenshots for docs

---

## Dependencies

Already in `pyproject.toml`:
```toml
tui = [
    "textual>=0.45.0",
    "rich>=14.0.0",
]
```

No new dependencies required.

Install: `pip install -e ".[tui]"`

---

## Testing Strategy

| Layer | What | How |
|-------|------|-----|
| Unit | ModeManager, DiffEngine, FileOps, Session | pytest, no Textual |
| Widget | Individual widgets render correctly | Textual's `pilot` test framework |
| Integration | Full app with mock BackendBridge | Async pilot tests |
| E2E | Real backend connection | Manual + optional CI |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Textual async conflicts with ObscuraClient async | BackendBridge runs client in `run_worker()` thread |
| Large diffs overwhelm terminal | Virtual scrolling in DiffView, max line limit |
| Session files grow unbounded | Auto-rotate: keep last 50 sessions, compress old ones |
| Backend disconnects mid-stream | Error widget + auto-reconnect with backoff |

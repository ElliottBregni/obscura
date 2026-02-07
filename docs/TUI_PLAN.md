# 📋 Obscura TUI Plan

> Claude Code-style Terminal UI for Obscura

---

## Overview

Build a **Textual-based TUI** that provides a Claude Code-style interactive experience for Obscura. This will be a new CLI subcommand: `obscura tui`

---

## Core Features

### 1. **Ask Mode** (Interactive Chat)
- REPL-style chat with agents
- Real-time streaming responses
- Context-aware (remembers conversation)
- Switch between agents mid-chat

### 2. **Plan Mode** (Task Planning)
- Break down complex tasks into steps
- Visual plan editor
- Track progress through steps
- Spawn sub-agents for each step

### 3. **Code Mode** (File Editing)
- View and edit files inline
- Syntax highlighting
- File tree navigation
- Search across codebase

### 4. **Diff Analysis**
- Side-by-side diff view
- Syntax-highlighted changes
- Accept/reject changes
- Batch operations

### 5. **Agent Dashboard**
- List all running agents
- Real-time status updates
- Memory inspection
- Logs streaming

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Obscura TUI App                          │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────────────────────────────┐ │
│  │   Sidebar    │  │           Main Content               │ │
│  │              │  │                                      │ │
│  │ - Agents     │  │  ┌────────────────────────────────┐  │ │
│  │ - Files      │  │  │      Chat/Editor/Diff          │  │ │
│  │ - Memory     │  │  │                                │  │ │
│  │ - Settings   │  │  │                                │  │ │
│  └──────────────┘  │  └────────────────────────────────┘  │ │
│                    │  ┌──────────────────────────────────┐ │ │
│                    │  │        Input/Command Bar         │ │ │
│                    │  └──────────────────────────────────┘ │ │
│                    └──────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼ HTTP/WebSocket
                   ┌──────────────────────┐
                   │   Obscura Server     │
                   │   (localhost:8080)   │
                   └──────────────────────┘
```

---

## Screens

### Screen 1: Chat Screen (`Ask Mode`)
```
┌────────────────────────────────────────────────────────────┐
│ 🤖 Obscura TUI - Agent: code-reviewer (claude)             │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  code-reviewer                                             │
│  > Review this PR for security issues                      │
│                                                            │
│  Looking at the changes...                                 │
│  [spinner]                                                 │
│                                                            │
│  Found 2 potential issues:                                 │
│  1. SQL injection in line 45                               │
│  2. Missing auth check in line 82                          │
│                                                            │
│  [Diff: src/api/users.py]                                  │
│  - query = f"SELECT * FROM users WHERE id = {user_id}"     │
│  + query = "SELECT * FROM users WHERE id = ?"              │
│                                                            │
├────────────────────────────────────────────────────────────┤
│  > _                                                       │
└────────────────────────────────────────────────────────────┘
```

### Screen 2: Plan Screen
```
┌────────────────────────────────────────────────────────────┐
│ 📋 Task Plan: Refactor authentication                      │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  [✓] 1. Analyze current auth flow                         │
│  [✓] 2. Identify security vulnerabilities                 │
│  [→] 3. Design new auth architecture  ← Current           │
│  [ ] 4. Implement JWT tokens                               │
│  [ ] 5. Add refresh token rotation                         │
│  [ ] 6. Update middleware                                  │
│  [ ] 7. Write tests                                        │
│                                                            │
│  Progress: 2/7 (29%)                                       │
│  Agents: 2 active                                          │
│                                                            │
├────────────────────────────────────────────────────────────┤
│  > next  │  spawn  │  edit  │  delete                     │
└────────────────────────────────────────────────────────────┘
```

### Screen 3: Code Editor
```
┌────────────────────────────────────────────────────────────┐
│ 📝 src/auth/middleware.py                                  │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  1  │ import jwt                                           │
│  2  │ from datetime import datetime, timedelta             │
│  3  │                                                      │
│  4  │ class AuthMiddleware:                              │
│  5  │     def __init__(self, secret_key):                │
│  6  │         self.secret_key = secret_key               │
│  7  │                                                      │
│  8  │     def verify_token(self, token):                 │
│  9  │         try:                                         │
│ 10  │             payload = jwt.decode(                   │
│ 11  │                 token,                              │
│ 12  │                 self.secret_key,                    │
│ 13  │                 algorithms=["HS256"]                │
│ 14  │             )                                        │
│ 15  │             return payload                          │
│ 16  │         except jwt.ExpiredSignatureError:          │
│ 17  │             return None                              │
│ 18  │                                                      │
├────────────────────────────────────────────────────────────┤
│  Line 10, Col 25  │  Python  │  Modified                   │
└────────────────────────────────────────────────────────────┘
```

### Screen 4: Diff View
```
┌────────────────────────────────────────────────────────────┐
│ 🔍 Diff: src/auth/middleware.py                            │
├────────────────────────────────────────────────────────────┤
│  Original                      │  Modified                  │
├────────────────────────────────┼────────────────────────────┤
│                                │  + import jwt              │
│                                │  + from datetime import    │
│  def check_auth(token):       │    def check_auth(token):  │
│      if not token:            │        if not token:       │
│          return False         │            return False    │
│                                │                            │
│  - query = f"SELECT *"        │  + query = "SELECT *"      │
│  -     "WHERE id = {id}"      │  +     "WHERE id = ?"      │
│                                │                            │
├────────────────────────────────────────────────────────────┤
│  [Accept All]  [Reject All]  [Accept]  [Reject]  [Next]   │
└────────────────────────────────────────────────────────────┘
```

### Screen 5: Agent Dashboard
```
┌────────────────────────────────────────────────────────────┐
│ 🤖 Active Agents                                           │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  ID          Name           Model      Status    Runtime   │
│  ──────────────────────────────────────────────────────────│
│  agent-1     code-reviewer  claude    🟢 Running  5m 32s   │
│  agent-2     doc-writer     claude    🟡 Waiting  2m 15s   │
│  agent-3     test-gen       copilot   🔴 Error    1m 45s   │
│                                                            │
│  [Spawn New]  [Stop All]  [View Logs]  [Memory]            │
│                                                            │
├────────────────────────────────────────────────────────────┤
│  Selected: code-reviewer  │  Memory: 12 entries            │
└────────────────────────────────────────────────────────────┘
```

---

## Implementation Phases

### Phase 1: Foundation (Day 1)
- [ ] Set up Textual app structure
- [ ] Create base screens (Chat, Plan, Code, Diff, Dashboard)
- [ ] Implement navigation between screens
- [ ] Add keyboard shortcuts

### Phase 2: Chat Screen (Day 2)
- [ ] Build chat interface with Rich
- [ ] Connect to WebSocket for streaming
- [ ] Add agent selector
- [ ] Message history with context

### Phase 3: Code & Diff (Day 3)
- [ ] File tree sidebar
- [ ] Syntax-highlighted editor view
- [ ] Diff view with side-by-side
- [ ] Accept/reject change actions

### Phase 4: Plan Mode (Day 4)
- [ ] Task plan visualization
- [ ] Progress tracking
- [ ] Spawn agents from plan
- [ ] Step-by-step execution

### Phase 5: Dashboard & Polish (Day 5)
- [ ] Agent dashboard with real-time updates
- [ ] Memory browser
- [ ] Settings panel
- [ ] Help system
- [ ] Tests & docs

---

## Key Bindings

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `Ctrl+T` | Toggle theme (dark/light) |
| `Tab` | Next widget |
| `Shift+Tab` | Previous widget |
| `F1` | Help |
| `F2` | Dashboard |
| `F3` | Chat |
| `F4` | Plan |
| `F5` | Code |
| `F6` | Diff |
| `Ctrl+N` | New agent |
| `Ctrl+S` | Stop agent |
| `Ctrl+M` | Memory browser |
| `/` | Search |
| `Esc` | Back/Cancel |

---

## Files to Create

```
obscura/tui/
├── __init__.py           # Package init
├── app.py                # Main TUI app
├── screens/
│   ├── __init__.py
│   ├── chat.py           # Chat screen
│   ├── plan.py           # Plan screen
│   ├── code.py           # Code editor
│   ├── diff.py           # Diff view
│   ├── dashboard.py      # Agent dashboard
│   └── settings.py       # Settings
├── widgets/
│   ├── __init__.py
│   ├── agent_list.py     # Agent list widget
│   ├── chat_log.py       # Chat message log
│   ├── code_editor.py    # Code editor widget
│   ├── diff_view.py      # Diff viewer widget
│   ├── file_tree.py      # File tree sidebar
│   └── status_bar.py     # Status bar
├── commands.py           # CLI commands integration
└── client.py             # Obscura API client for TUI
```

---

## Integration with Existing Code

### New CLI Command
```python
# In obscura_cli.py
@cli.command("tui")
def tui():
    """Launch interactive TUI."""
    from obscura.tui.app import TUIApp
    app = TUIApp()
    app.run()
```

### Dependencies
```toml
[project.optional-dependencies]
tui = [
    "textual>=0.45.0",      # TUI framework
    "textual-textarea>=0",   # Code editing (optional)
]
```

---

## Testing

- Unit tests for each widget
- Screen navigation tests
- Keyboard shortcut tests
- Integration tests with mocked API
- E2E tests with real server

---

## Future Enhancements

1. **Multi-pane layout** - Chat + Code side-by-side
2. **Git integration** - View commits, branches
3. **Terminal emulator** - Run commands inline
4. **Image preview** - For vision models
5. **Voice input** - Speech-to-text

---

## Success Criteria

- [ ] All 5 screens functional
- [ ] Smooth navigation between modes
- [ ] Real-time streaming works
- [ ] Diff view handles large files
- [ ] Keyboard shortcuts intuitive
- [ ] Tests >80% coverage
- [ ] Documentation complete

---

Ready to start implementation? I can begin with Phase 1 (Foundation) right now.

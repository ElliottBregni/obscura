# UAT Validation Plan -- Obscura TUI

**Document**: UAT_TUI.md
**Component**: `sdk/tui/` -- Terminal User Interface
**Plan Reference**: `PLAN_TUI.md`
**Date**: 2026-02-07
**Status**: Pre-implementation

---

## 1. Acceptance Criteria Matrix

### Phase 1: Core Shell

| ID | Feature | Acceptance Criteria | Priority | Status |
|----|---------|-------------------|----------|--------|
| UAT-001 | TUI Launch | `obscura-sdk tui` starts a Textual application without errors. Default mode is Ask. Sidebar, message list, input area, and status bar are all rendered. | P0 | [ ] Not tested |
| UAT-002 | Backend Connection | `BackendBridge.connect()` initializes an `ObscuraClient` with the specified backend (`claude` or `copilot`). Connection errors surface a clear message in the UI, not a stack trace. | P0 | [ ] Not tested |
| UAT-003 | Streaming Responses | User submits a prompt; `ChunkKind.TEXT_DELTA` chunks stream into a `MessageBubble` in real time. The UI remains responsive during streaming (input area does not freeze). | P0 | [ ] Not tested |
| UAT-004 | Markdown Rendering | Assistant responses render Rich Markdown: headers, bold, italics, lists, and inline code. Code blocks are syntax-highlighted via Rich. | P0 | [ ] Not tested |
| UAT-005 | Status Bar -- Mode & Model | Status bar displays the current mode tag (e.g. `[ASK]`) and the active backend/model string (e.g. `claude-sonnet`). Both update when mode or backend changes. | P0 | [ ] Not tested |
| UAT-006 | Input Area -- Prompt Prefix | Input area shows a mode-specific prefix: `[ASK]>`, `[PLAN]>`, `[CODE]>`, `[DIFF]>`. Prefix updates immediately on mode switch. | P0 | [ ] Not tested |
| UAT-007 | Input Area -- Submit & Newline | `Enter` submits the current prompt. `Shift+Enter` inserts a newline without submitting. | P0 | [ ] Not tested |
| UAT-008 | Message List -- Scrolling | Message list auto-scrolls to the bottom as new content streams in. User can scroll up to review history; auto-scroll resumes when scrolled back to the bottom. | P1 | [ ] Not tested |
| UAT-009 | CLI Subcommand -- `tui` | `obscura-sdk tui` is registered in `build_parser()`. Arguments `--backend`, `--model`, `--cwd`, `--session`, `--mode` are parsed correctly and forwarded to `ObscuraTUI.__init__`. | P0 | [ ] Not tested |
| UAT-010 | App Quit | `Ctrl+Q` cleanly shuts down the app: `BackendBridge.disconnect()` is called, Textual event loop exits, terminal state is restored. | P0 | [ ] Not tested |
| UAT-011 | Dark Theme | Default CSS theme renders with dark background, readable foreground, and correct widget borders. No unstyled/broken widgets on launch. | P1 | [ ] Not tested |

### Phase 2: Plan Mode + Thinking Blocks

| ID | Feature | Acceptance Criteria | Priority | Status |
|----|---------|-------------------|----------|--------|
| UAT-012 | Thinking Blocks -- Collapsed | `ChunkKind.THINKING_DELTA` chunks render inside a `ThinkingBlock` widget that is collapsed by default. Collapsed state shows a summary line (e.g. "Thinking...") without exposing the full content. | P0 | [ ] Not tested |
| UAT-013 | Thinking Blocks -- Expand/Collapse | Pressing `t` while a `ThinkingBlock` is focused expands it to show the full reasoning content. Pressing `t` again collapses it. | P0 | [ ] Not tested |
| UAT-014 | Tool Use -- Status Indicators | `ChunkKind.TOOL_USE_START` renders a `ToolStatus` widget with the tool name and a spinner indicator. `ChunkKind.TOOL_RESULT` replaces the spinner with a success checkmark or failure cross. | P0 | [ ] Not tested |
| UAT-015 | Tool Use -- Input Delta | `ChunkKind.TOOL_USE_DELTA` progressively updates the `ToolStatus` widget to show the tool's input being constructed. | P1 | [ ] Not tested |
| UAT-016 | Plan Mode -- Switch | `Ctrl+P` switches to Plan mode. Status bar updates to `[PLAN]`. Input prefix changes to `[PLAN]>`. Sidebar mode selector highlights Plan. | P0 | [ ] Not tested |
| UAT-017 | Plan Mode -- Structured Response | In Plan mode, the system prompt guides the agent to produce numbered steps. The `PlanView` widget renders these as a checklist with step numbers and descriptions. | P0 | [ ] Not tested |
| UAT-018 | Plan Mode -- Approve/Reject Steps | Each step in `PlanView` has approve (y) and reject (n) controls. Approved steps show a checkmark; rejected steps show a cross. Summary bar updates (e.g. "3/5 steps approved"). | P0 | [ ] Not tested |
| UAT-019 | Plan Mode -- Execute Plan | When all steps have been decided, an "Execute Plan" action becomes available. Triggering it stores the approved plan in `ModeManager.active_plan` and transitions to Code mode. | P1 | [ ] Not tested |
| UAT-020 | Plan Mode -- Edit Steps | User can edit a plan step's description inline before approving or rejecting it. Edited content persists in the plan. | P2 | [ ] Not tested |
| UAT-021 | Sidebar -- Mode Selector | Sidebar shows radio-button-style mode selector with Ask, Plan, Code, Diff. Clicking a mode or pressing its shortcut activates it. Active mode is visually distinct. | P0 | [ ] Not tested |
| UAT-022 | Sidebar -- Backend Info | Sidebar displays the active backend name and model ID. Values update if `/backend` or `/model` slash commands are used. | P1 | [ ] Not tested |
| UAT-023 | Sidebar -- Session Info | Sidebar shows the current session ID, turn count, and elapsed time. Values update after each conversation turn. | P1 | [ ] Not tested |

### Phase 3: Code Mode + Diff Engine

| ID | Feature | Acceptance Criteria | Priority | Status |
|----|---------|-------------------|----------|--------|
| UAT-024 | Code Mode -- Switch | `Ctrl+E` switches to Code mode. Status bar, input prefix, and sidebar all update accordingly. | P0 | [ ] Not tested |
| UAT-025 | Code Mode -- File Read Tool | Agent can use a `read_file` tool; the `ToolStatus` widget shows the file path and contents are available for the agent's context. | P0 | [ ] Not tested |
| UAT-026 | Code Mode -- File Write Tool | Agent can use a `write_file` tool. The resulting `FileChange` is captured by `DiffEngine.compute()`, producing hunks. The change appears as an inline diff in the message view. | P0 | [ ] Not tested |
| UAT-027 | Code Mode -- Accept Change | User can accept a file change. `FileOps.write()` commits the content to disk. `FileOps.backup()` stores the original version in `.obscura_backups/`. The change status becomes "accepted". | P0 | [ ] Not tested |
| UAT-028 | Code Mode -- Reject Change | User can reject a file change. The modified content is discarded; the original file remains untouched. Feedback is sent to the agent (via the conversation context) that the change was rejected. | P0 | [ ] Not tested |
| UAT-029 | Code Mode -- Inline Diff Display | Diffs display with green for additions and red for deletions. Line numbers appear in the gutter. The diff is readable and correctly aligned. | P0 | [ ] Not tested |
| UAT-030 | Code Mode -- File Tree in Sidebar | Sidebar shows a `FileTree` widget listing all files modified in the current session. Each file shows its change status (pending/accepted/rejected). | P1 | [ ] Not tested |
| UAT-031 | Diff Engine -- Hunk Computation | `DiffEngine.compute(original, modified)` produces a correct list of `DiffHunk` objects. Each hunk has accurate `old_start`, `old_count`, `new_start`, `new_count`, and line-tagged `DiffLine` entries. | P0 | [ ] Not tested |
| UAT-032 | Diff Engine -- Hunk Application | `DiffEngine.apply_hunks(original, accepted_hunks)` produces a correct merged result when only a subset of hunks are accepted. | P0 | [ ] Not tested |
| UAT-033 | File Ops -- Backup/Restore | `FileOps.backup()` creates a timestamped copy in `.obscura_backups/`. `FileOps.restore()` replaces the current file with the backup. Backup paths are deterministic and do not collide. | P0 | [ ] Not tested |
| UAT-034 | File Ops -- Path Safety | `FileOps` rejects paths that escape the configured `cwd` via `../` traversal. Absolute paths outside `cwd` are rejected with a clear error. | P0 | [ ] Not tested |

### Phase 4: Diff Mode + Session Persistence

| ID | Feature | Acceptance Criteria | Priority | Status |
|----|---------|-------------------|----------|--------|
| UAT-035 | Diff Mode -- Switch | `Ctrl+D` switches to Diff mode. The main panel displays the `DiffView` widget with all pending and accepted changes from Code mode. | P0 | [ ] Not tested |
| UAT-036 | Diff Mode -- Hunk Navigation | `j` moves focus to the next hunk; `k` moves to the previous hunk. Current hunk is visually highlighted. Navigation wraps or stops at boundaries (consistent behavior). | P0 | [ ] Not tested |
| UAT-037 | Diff Mode -- Per-Hunk Accept/Reject | `a` accepts the focused hunk; `r` rejects it. Status indicators update inline. The file's merged result is recomputed after each decision. | P0 | [ ] Not tested |
| UAT-038 | Diff Mode -- File-Level Accept/Reject | `A` accepts all hunks in the focused file; `R` rejects all. Status of every hunk in the file updates. | P1 | [ ] Not tested |
| UAT-039 | Diff Mode -- Unified/Side-by-Side Toggle | `d` toggles between unified diff and side-by-side diff rendering. Both views are correct and legible. Side-by-side view adapts to terminal width. | P1 | [ ] Not tested |
| UAT-040 | Diff Mode -- File List with Status | Sidebar shows the list of files with status icons: checkmark for accepted, cross for rejected, circle for pending. Icons update in real time as hunks are decided. | P1 | [ ] Not tested |
| UAT-041 | Session Persistence -- Save | `TUISession.save()` writes conversation history to `~/.obscura/tui_sessions/<session_id>.json`. The JSON contains all `ConversationTurn` objects with role, content, timestamp, mode, and metadata. | P0 | [ ] Not tested |
| UAT-042 | Session Persistence -- Load | `TUISession.load(session_id)` reads the JSON file and reconstructs the `TUISession` with all turns intact. Loaded turns render in the message list as if the conversation had occurred in the current session. | P0 | [ ] Not tested |
| UAT-043 | Session Persistence -- List | `TUISession.list_sessions()` returns metadata for all saved sessions, sorted by most recent. Each entry includes session ID, turn count, last active timestamp, and last mode used. | P1 | [ ] Not tested |
| UAT-044 | Session Persistence -- Resume via CLI | `obscura-sdk tui --session <id>` launches the TUI and loads the specified session. Conversation history is displayed. The user can continue from where they left off. | P0 | [ ] Not tested |
| UAT-045 | Slash Command -- `/mode` | `/mode ask`, `/mode plan`, `/mode code`, `/mode diff` each switch to the specified mode. Invalid mode names produce an error message in the message list. | P0 | [ ] Not tested |
| UAT-046 | Slash Command -- `/backend` | `/backend claude` and `/backend copilot` switch the active backend. The bridge disconnects the current client and connects a new one. Status bar and sidebar update. | P1 | [ ] Not tested |
| UAT-047 | Slash Command -- `/model` | `/model <model-id>` changes the model. Takes effect on the next prompt. Status bar updates to show the new model. | P1 | [ ] Not tested |
| UAT-048 | Slash Command -- `/session new` | Creates a new session, clears the message list, resets mode to Ask. Previous session is auto-saved if it has any turns. | P0 | [ ] Not tested |
| UAT-049 | Slash Command -- `/session list` | Displays a formatted list of saved sessions in the message area. Each entry shows ID, turn count, and last active time. | P1 | [ ] Not tested |
| UAT-050 | Slash Command -- `/session load <id>` | Loads the specified session. Conversation history renders. Mode is restored to the mode of the last turn. | P0 | [ ] Not tested |
| UAT-051 | Slash Command -- `/clear` | Clears the message list and resets in-memory conversation. Does not delete the saved session file. | P0 | [ ] Not tested |
| UAT-052 | Slash Command -- `/memory list` | Displays all memory namespace keys in the message area. | P2 | [ ] Not tested |
| UAT-053 | Slash Command -- `/memory get <key>` | Displays the value of the specified memory key. Invalid keys produce an error message. | P2 | [ ] Not tested |
| UAT-054 | Slash Command -- `/diff show` | Displays all pending diffs in the message area, even when not in Diff mode. | P1 | [ ] Not tested |
| UAT-055 | Slash Command -- `/diff accept-all` | Accepts all pending changes across all files. Each file's changes are written to disk with backup. Confirmation message shown. | P1 | [ ] Not tested |
| UAT-056 | Slash Command -- `/diff reject-all` | Rejects all pending changes across all files. No files are modified. Confirmation message shown. | P1 | [ ] Not tested |
| UAT-057 | Slash Command -- `/help` | Displays a formatted help message listing all slash commands, keybindings, and modes. | P0 | [ ] Not tested |
| UAT-058 | Slash Command -- `/quit` | Exits the TUI cleanly, equivalent to `Ctrl+Q`. | P1 | [ ] Not tested |

### Phase 5: Polish

| ID | Feature | Acceptance Criteria | Priority | Status |
|----|---------|-------------------|----------|--------|
| UAT-059 | Input History | `Up` arrow recalls the previous input; `Down` arrow moves forward in history. History persists across mode switches within a session but resets on new session. | P1 | [ ] Not tested |
| UAT-060 | File Path Autocomplete | In Code mode, pressing `Tab` triggers file path completion relative to `cwd`. Partial paths are expanded; multiple matches show a dropdown. | P2 | [ ] Not tested |
| UAT-061 | Memory Browser | Sidebar includes an expandable memory namespace browser. Keys are listed, and expanding a key shows its value. | P2 | [ ] Not tested |
| UAT-062 | Responsive Layout | The layout adjusts to terminal sizes as small as 80x24. Sidebar can be hidden via `Ctrl+B` to free horizontal space. Panels do not overlap or overflow. | P1 | [ ] Not tested |
| UAT-063 | Error Recovery -- Backend Disconnect | If the backend disconnects mid-stream, an error widget appears with the error message. The app offers to reconnect with backoff. Input area remains functional. | P1 | [ ] Not tested |
| UAT-064 | Graceful Degradation -- No Backend | If no backend is configured or auth fails at launch, the TUI starts in an offline mode where the user can review previous sessions. A banner explains the connection failure. | P2 | [ ] Not tested |
| UAT-065 | Cancel Stream | `Escape` (or `Ctrl+C`) cancels the in-progress stream. The partial response remains visible in the message list. The agent does not receive additional chunks. The input area becomes active again. | P0 | [ ] Not tested |
| UAT-066 | Session Auto-Rotate | When the count of saved sessions exceeds 50, the oldest sessions are compressed or pruned. The user is not prompted; this is automatic. | P2 | [ ] Not tested |

---

## 2. User Journey Tests

### Journey 1: First Launch

| Step | User Action | Expected Result |
|------|-------------|-----------------|
| 1.1 | Run `obscura-sdk tui` | Textual app launches. Terminal enters alternate screen mode. |
| 1.2 | Observe layout | Four regions visible: Sidebar (left), MessageList (center), PromptInput (bottom center), StatusBar (bottom full-width). |
| 1.3 | Check sidebar | Mode selector shows Ask highlighted. Backend displays `claude`. Session shows a new auto-generated ID. |
| 1.4 | Check status bar | Displays `[ASK] claude` (or `claude-sonnet` if model resolved). Timer shows `0.0s`. |
| 1.5 | Check input area | Cursor is blinking in the input area. Prefix shows `[ASK]>`. |
| 1.6 | Press `Ctrl+Q` | App exits cleanly. Terminal restores original state. No error output. |

### Journey 2: Ask Mode Conversation

| Step | User Action | Expected Result |
|------|-------------|-----------------|
| 2.1 | Type "What is Rust?" and press `Enter` | User message appears right-aligned (or distinctly styled) in message list. Input area clears. |
| 2.2 | Observe streaming | Assistant response streams character-by-character into a new `MessageBubble`. Markdown renders incrementally. |
| 2.3 | Wait for completion | `ChunkKind.DONE` triggers `MessageBubble.finalize()`. Status bar timer shows elapsed time (e.g. `2.3s`). |
| 2.4 | Scroll up | Message list scrolls. User can review the full response. |
| 2.5 | Scroll back to bottom | Auto-scroll resumes for the next message. |
| 2.6 | If response includes code block | Code block has syntax highlighting with language label. Background color differentiates it from prose. |
| 2.7 | If response triggers thinking | `ThinkingBlock` appears collapsed. Pressing `t` expands to show reasoning. Pressing `t` again collapses. |
| 2.8 | If response triggers tool use | `ToolStatus` shows tool name with spinner. On result, spinner becomes checkmark or cross. |

### Journey 3: Plan Mode Workflow

| Step | User Action | Expected Result |
|------|-------------|-----------------|
| 3.1 | Press `Ctrl+P` | Mode switches to Plan. Status bar shows `[PLAN]`. Input prefix changes to `[PLAN]>`. Sidebar highlights Plan. |
| 3.2 | Type "Add dark mode to the settings page" and press `Enter` | Prompt submitted. System prompt includes Plan mode instructions. |
| 3.3 | Observe response | Agent responds with numbered steps. `PlanView` widget renders a checklist. |
| 3.4 | Review step 1 | Step shows number, description, and approve/reject controls. |
| 3.5 | Approve step 1 (press `y`) | Checkmark appears next to step 1. Summary bar updates to "1/N steps approved". |
| 3.6 | Reject step 3 (press `n`) | Cross appears next to step 3. Summary updates. |
| 3.7 | Decide all steps | "Execute Plan" action becomes available. |
| 3.8 | Trigger "Execute Plan" | Approved plan is stored in `ModeManager.active_plan`. App transitions to Code mode automatically. Status bar shows `[CODE]`. |

### Journey 4: Code Mode File Changes

| Step | User Action | Expected Result |
|------|-------------|-----------------|
| 4.1 | (Continuing from Journey 3) Observe Code mode | Input prefix is `[CODE]>`. System prompt includes Code mode instructions. The approved plan is included in context. |
| 4.2 | Agent uses `read_file` tool | `ToolStatus` shows "read_file: path/to/file" with spinner, then checkmark. File contents are in the agent's context. |
| 4.3 | Agent uses `write_file` tool | `DiffEngine` computes hunks between original and modified. Inline diff appears in the message area showing additions (green) and deletions (red) with line numbers. |
| 4.4 | Accept the change | `FileOps.write()` writes the modified content. `FileOps.backup()` stores original in `.obscura_backups/`. Change status becomes "accepted". |
| 4.5 | Reject a different change | Original file untouched. Change status becomes "rejected". Agent receives feedback that the change was rejected. |
| 4.6 | Check sidebar | `FileTree` widget lists modified files. Accepted files show checkmark, rejected show cross, pending show circle. |

### Journey 5: Diff Mode Review

| Step | User Action | Expected Result |
|------|-------------|-----------------|
| 5.1 | Press `Ctrl+D` | Mode switches to Diff. `DiffView` widget displays all pending/accepted/rejected changes from Code mode. |
| 5.2 | View unified diff | Default view is unified. Additions in green, deletions in red, context lines in default color. Line numbers in gutter. |
| 5.3 | Press `d` | View toggles to side-by-side. Original on left, modified on right. Aligned by line number. |
| 5.4 | Press `d` again | View returns to unified. |
| 5.5 | Press `j` | Focus moves to the next hunk. Highlight updates. |
| 5.6 | Press `k` | Focus moves to the previous hunk. |
| 5.7 | Press `a` on a pending hunk | Hunk is accepted. Status icon updates. File's merged result recomputes. |
| 5.8 | Press `r` on a pending hunk | Hunk is rejected. Status icon updates. |
| 5.9 | Press `A` | All hunks in the current file are accepted. |
| 5.10 | Press `R` | All hunks in the current file are rejected. |
| 5.11 | Check sidebar | File list shows updated status icons matching the accept/reject decisions. |

### Journey 6: Session Persistence

| Step | User Action | Expected Result |
|------|-------------|-----------------|
| 6.1 | Send several messages in Ask mode, switch to Plan mode, send more | Conversation history accumulates with correct mode tags on each turn. |
| 6.2 | Type `/session new` | Current session is auto-saved to `~/.obscura/tui_sessions/<old_id>.json`. Message list clears. New session ID is generated. Sidebar updates. |
| 6.3 | Type `/session list` | List of saved sessions appears in message area with session IDs, turn counts, and timestamps. The previous session appears. |
| 6.4 | Press `Ctrl+Q` | App exits. Session is saved. |
| 6.5 | Run `obscura-sdk tui --session <old_id>` | App launches. Previous conversation history loads and renders in message list. Mode is restored to the mode of the last turn. |
| 6.6 | Send a follow-up message | Agent has the full conversation context from the loaded session. Response is contextually correct. |

### Journey 7: Slash Commands

| Step | Command | Expected Result |
|------|---------|-----------------|
| 7.1 | `/mode ask` | Switches to Ask mode. |
| 7.2 | `/mode plan` | Switches to Plan mode. |
| 7.3 | `/mode code` | Switches to Code mode. |
| 7.4 | `/mode diff` | Switches to Diff mode. |
| 7.5 | `/mode invalid` | Error message: "Unknown mode: invalid. Valid modes: ask, plan, code, diff". |
| 7.6 | `/backend copilot` | Backend switches. Bridge reconnects. Status bar updates. |
| 7.7 | `/backend invalid` | Error message: "Unknown backend: invalid. Valid backends: claude, copilot". |
| 7.8 | `/model gpt-5-mini` | Model changes. Status bar updates. Next prompt uses new model. |
| 7.9 | `/session new` | New session created. Message list clears. |
| 7.10 | `/session list` | Sessions listed with metadata. |
| 7.11 | `/session load <id>` | Session loaded. History restored. |
| 7.12 | `/session load nonexistent` | Error: "Session not found: nonexistent". |
| 7.13 | `/clear` | Message list clears. In-memory history resets. Session file not deleted. |
| 7.14 | `/memory list` | Memory keys displayed. |
| 7.15 | `/memory get <key>` | Memory value displayed for valid key. |
| 7.16 | `/memory get nonexistent` | Error: "Memory key not found: nonexistent". |
| 7.17 | `/diff show` | Pending diffs rendered in message area. |
| 7.18 | `/diff accept-all` | All pending changes accepted and written to disk. |
| 7.19 | `/diff reject-all` | All pending changes rejected. |
| 7.20 | `/help` | Help text rendered with all commands, keybindings, and mode descriptions. |
| 7.21 | `/quit` | App exits cleanly. |
| 7.22 | `/unknown` | Error: "Unknown command: /unknown. Type /help for available commands." |
| 7.23 | `/` (empty) | No action or shows autocomplete suggestions. |

---

## 3. UX Validation Checklist

### Keyboard Shortcuts

- [ ] `Enter` submits prompt from input area
- [ ] `Shift+Enter` inserts a newline without submitting
- [ ] `Ctrl+A` switches to Ask mode
- [ ] `Ctrl+P` switches to Plan mode
- [ ] `Ctrl+E` switches to Code mode
- [ ] `Ctrl+D` switches to Diff mode
- [ ] `Ctrl+B` toggles sidebar visibility
- [ ] `Ctrl+N` creates a new session
- [ ] `Ctrl+C` cancels the current stream
- [ ] `Ctrl+Q` quits the application
- [ ] `Escape` cancels stream or closes modal
- [ ] `t` toggles thinking block expand/collapse
- [ ] `j` navigates to next hunk in Diff mode
- [ ] `k` navigates to previous hunk in Diff mode
- [ ] `a` accepts focused hunk in Diff mode
- [ ] `r` rejects focused hunk in Diff mode
- [ ] `A` accepts all hunks in focused file
- [ ] `R` rejects all hunks in focused file
- [ ] `d` toggles unified/side-by-side diff view
- [ ] `Up`/`Down` arrows navigate command history in input area
- [ ] `Tab` triggers file path autocomplete in Code mode

### Visual & Interaction

- [ ] Mode switching is instant (sub-100ms visual update)
- [ ] Mode switch updates: status bar, input prefix, sidebar highlight -- all simultaneously
- [ ] Streaming responses do not freeze input area or sidebar
- [ ] User can type during streaming (input is buffered)
- [ ] Error messages appear inline in the message list with distinct styling (e.g. red border)
- [ ] Error messages include actionable guidance (e.g. "Check your API key" not just "Auth failed")
- [ ] Sidebar information stays current after each turn (turn count, timing, session ID)
- [ ] Status bar reflects current state: mode tag, model name, last request duration
- [ ] Large diffs (500+ lines) scroll smoothly without visual tearing
- [ ] File tree in Code mode updates after each file operation
- [ ] Input history (up/down) preserves multi-line entries correctly
- [ ] Slash command autocomplete shows matching commands after `/`
- [ ] User and assistant messages are visually distinct (color, alignment, or border)
- [ ] Long single-line outputs word-wrap correctly within the message bubble
- [ ] Empty state: message list shows a welcome message or hint on first launch

---

## 4. Edge Case Scenarios

### Network & Backend

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-01 | Backend disconnects mid-stream | Partial response remains visible. Error widget appears below the partial content with the error message. App offers reconnect. Input area reactivates. |
| EC-02 | Backend returns HTTP 401 (auth expired) | Error message: "Authentication failed. Please re-authenticate." No retry loop. User can fix auth and use `/backend` to reconnect. |
| EC-03 | Backend returns HTTP 429 (rate limited) | Error message includes the retry-after duration if available. App does not auto-retry. |
| EC-04 | No backend configured at launch | TUI starts in offline mode. Banner: "No backend configured. Use /backend to connect." Session review is available. |
| EC-05 | DNS resolution failure | Error message: "Unable to reach backend. Check your network connection." |
| EC-06 | Backend returns empty response (no chunks) | `ChunkKind.DONE` arrives with no preceding `TEXT_DELTA`. Message bubble shows "(empty response)" placeholder. |

### Input & Rendering

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-07 | Very long response (10k+ tokens) | Message bubble renders progressively. No memory spike. Scrolling remains smooth. |
| EC-08 | Very long single-line input (1000+ chars) | Input area wraps text. Submission works correctly. |
| EC-09 | Rapid mode switching during stream | Mode switches immediately. Stream continues in background and renders in the message list for the mode in which it was initiated. No crash. |
| EC-10 | Paste large text (10k+ chars) into input | Input area handles the paste without freezing. Submit sends the full text. |
| EC-11 | Unicode/emoji in prompt and response | Rendered correctly. No encoding errors. Character width calculations are correct for CJK and emoji. |
| EC-12 | Response containing raw ANSI escape codes | Escape codes are sanitized or rendered harmlessly. No terminal corruption. |
| EC-13 | Markdown with deeply nested structures | Renders without stack overflow or excessive indentation. Graceful fallback for unsupported nesting. |

### Slash Commands

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-14 | Invalid slash command | Error message: "Unknown command: /<command>. Type /help for available commands." |
| EC-15 | Slash command with extra whitespace | Command is trimmed and parsed correctly (e.g. `/mode  ask ` works). |
| EC-16 | Slash command mid-stream | Command is queued or rejected with message: "Cannot run commands while a response is streaming. Cancel the stream first." |
| EC-17 | `/session load` with no argument | Error message: "Usage: /session load <session_id>". |

### File Operations (Code Mode)

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-18 | File operation on read-only file | Error message: "Permission denied: <path> is read-only." Change is marked as failed. Agent is informed. |
| EC-19 | File operation outside `cwd` | Rejected by `FileOps` path safety check. Error: "Path <path> is outside the working directory." |
| EC-20 | File operation on nonexistent file (write) | New file is created. Diff shows all lines as additions. Backup directory records that the file did not previously exist. |
| EC-21 | File operation on nonexistent file (read) | `ToolStatus` shows failure. Error sent to agent: "File not found: <path>". |
| EC-22 | Binary file modification attempted | Diff engine detects binary content (null bytes). Error: "Binary files cannot be diffed." Change is rejected. |
| EC-23 | File with very long lines (10k+ chars per line) | Diff view truncates or wraps long lines. No horizontal scroll lockup. |
| EC-24 | Simultaneous writes to the same file | Second write waits for first to complete (serialized). No data corruption. |
| EC-25 | Backup directory `.obscura_backups/` does not exist | `FileOps.backup()` creates it automatically before writing the backup. |

### Session Persistence

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-26 | Session file corrupted (invalid JSON) on load | Error message: "Session <id> is corrupted and cannot be loaded." App continues with a new session. |
| EC-27 | Session directory `~/.obscura/tui_sessions/` does not exist | Created automatically on first save. |
| EC-28 | Session file with incompatible schema version | Error with guidance: "Session <id> was created with an older version. Cannot load." |
| EC-29 | Disk full during session save | Error message: "Failed to save session: disk full." In-memory session remains intact. |
| EC-30 | Two TUI instances writing the same session | Last-write-wins semantics. No crash. Optional: file locking warning. |

### Terminal Environment

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-31 | Terminal resize during operation | Layout reflows. Widgets resize. No content loss. Diff view recalculates side-by-side column widths. |
| EC-32 | Very small terminal (40x12) | Sidebar auto-hides. Message list and input area remain usable. Warning banner if terminal is too small for Diff view. |
| EC-33 | Terminal does not support true color | Graceful fallback to 256-color or 16-color theme. No invisible text. |
| EC-34 | Running inside tmux/screen | Mouse events and key bindings work correctly. No escape sequence conflicts. |
| EC-35 | SSH session with high latency | Rendering does not buffer excessively. Input remains responsive. |

### Concurrency

| ID | Scenario | Expected Behavior |
|----|----------|-------------------|
| EC-36 | User submits a second prompt while first is streaming | Second prompt is queued or rejected with message: "Please wait for the current response to complete, or press Escape to cancel." No concurrent streams. |
| EC-37 | Cancel stream then immediately submit new prompt | Previous stream is fully cancelled. New prompt starts cleanly. No interleaved chunks. |

---

## 5. Integration Validation

### 5.1 `sdk/tui/` and `sdk/client.py` Import Chain

| Check | Description | Validation Method |
|-------|-------------|-------------------|
| INT-01 | `BackendBridge` imports `ObscuraClient` from `sdk.client` | Unit test: import `BackendBridge`, verify `ObscuraClient` is accessible. |
| INT-02 | `BackendBridge.__init__` accepts `backend: str`, `model: str | None`, `cwd: str | None` and forwards them to `ObscuraClient(backend, model=model, cwd=cwd)` | Unit test: mock `ObscuraClient.__init__`, verify arguments. |
| INT-03 | `BackendBridge.connect()` calls `await client.start()` | Unit test: mock `ObscuraClient.start()`, verify it is awaited. |
| INT-04 | `BackendBridge.disconnect()` calls `await client.stop()` | Unit test: mock `ObscuraClient.stop()`, verify it is awaited. |
| INT-05 | `BackendBridge.stream_prompt()` iterates `client.stream(prompt)` | Integration test with mock backend: verify all `StreamChunk` objects are dispatched to callbacks. |

### 5.2 `ChunkKind` Routing (Plan vs. Implementation)

The plan specifies the following mapping. Each must be verified:

| ChunkKind | Plan Target | Validation |
|-----------|------------|------------|
| `ChunkKind.TEXT_DELTA` | `MessageBubble.append_text(chunk.text)` | Stream a `TEXT_DELTA` chunk; verify `MessageBubble` content updates. |
| `ChunkKind.THINKING_DELTA` | `ThinkingBlock.append(chunk.text)` | Stream a `THINKING_DELTA` chunk; verify `ThinkingBlock` content grows. |
| `ChunkKind.TOOL_USE_START` | `ToolStatus.start(chunk.tool_name)` | Stream a `TOOL_USE_START` chunk; verify `ToolStatus` widget appears with tool name and spinner. |
| `ChunkKind.TOOL_USE_DELTA` | `ToolStatus.update(chunk.tool_input_delta)` | Stream a `TOOL_USE_DELTA` chunk; verify `ToolStatus` input display updates. |
| `ChunkKind.TOOL_RESULT` | `ToolStatus.complete(chunk.text)` | Stream a `TOOL_RESULT` chunk; verify spinner replaced by result indicator. |
| `ChunkKind.DONE` | `MessageBubble.finalize()` + `StatusBar.update_timing()` | Stream a `DONE` chunk; verify bubble is finalized and status bar shows elapsed time. |
| `ChunkKind.ERROR` | `MessageBubble.show_error(chunk.text)` | Stream an `ERROR` chunk; verify error message appears with distinct styling. |

**Note**: The plan lists `TOOL_USE_DELTA` in the mapping but `stream_prompt()` callbacks only show `on_tool_start` and `on_tool_result`. The implementation must either add an `on_tool_delta` callback or handle `TOOL_USE_DELTA` within the existing `on_tool_start`/`on_tool_result` flow. This is a gap to track.

### 5.3 CLI `tui` Subcommand

| Check | Description | Validation Method |
|-------|-------------|-------------------|
| INT-06 | `build_parser()` includes `tui` subparser | Unit test: call `build_parser()`, parse `["tui"]`, verify `args.command == "tui"`. |
| INT-07 | `tui` subparser accepts `--backend`, `--model`, `--cwd`, `--session`, `--mode` | Unit test: parse each flag, verify `args` namespace. |
| INT-08 | `main()` routes `args.command == "tui"` to `_run_tui(args)` | Unit test: mock `_run_tui`, call `main(["tui"])`, verify `_run_tui` was called. |
| INT-09 | `_run_tui` instantiates `ObscuraTUI` and calls `app.run()` | Unit test: mock `ObscuraTUI`, verify constructor args and `run()` call. |
| INT-10 | `--mode` flag sets the initial mode to the specified value | Pass `--mode plan`; verify `ModeManager.current == TUIMode.PLAN` on startup. |

### 5.4 Textual CSS Themes

| Check | Description | Validation Method |
|-------|-------------|-------------------|
| INT-11 | `themes.py` exports at least one CSS string loadable by `App.CSS` | Unit test: import theme, verify it is a non-empty string containing Textual CSS selectors. |
| INT-12 | Dark theme renders: sidebar background, message bubbles, input area border, status bar background | Pilot test: mount `ObscuraTUI`, snapshot, verify no unstyled widgets. |
| INT-13 | Theme does not produce Textual CSS parse warnings | Pilot test: capture stderr during `app.run()`, verify no CSS warnings. |

### 5.5 Session JSON Format

| Check | Description | Validation Method |
|-------|-------------|-------------------|
| INT-14 | Session JSON is valid JSON | Save a session with multiple turns, read the file, parse with `json.loads()`. |
| INT-15 | Session JSON contains required fields | Verify presence of: `session_id`, `turns` (array), each turn having `role`, `content`, `timestamp`, `mode`, `metadata`. |
| INT-16 | `timestamp` is ISO 8601 formatted | Parse each turn's timestamp with `datetime.fromisoformat()`. |
| INT-17 | `mode` values are valid `TUIMode` enum values | Verify each turn's mode is one of: `"ask"`, `"plan"`, `"code"`, `"diff"`. |
| INT-18 | Session JSON is portable | Save a session on one machine, copy the JSON file to another, load it successfully. (Manual test.) |
| INT-19 | `metadata` captures tool use details | After a Code mode turn with tool use, verify metadata contains `tool_uses` with `tool_name`, `tool_input`, and `tool_result`. |

---

## 6. Regression Risk Areas

### 6.1 Async Streaming + Textual Event Loop

**Risk**: `ObscuraClient.stream()` is an `async for` generator. Textual runs its own asyncio event loop. If the backend bridge does not properly integrate (e.g. blocking the Textual loop), the UI freezes during streaming.

**Indicators of regression**:
- UI freezes while assistant response streams
- Input area becomes unresponsive during streaming
- `Escape` / `Ctrl+C` cancel does not work during stream
- Textual's internal timers (e.g. cursor blink) stop during stream

**Mitigation verification**:
- Confirm `BackendBridge` uses `run_worker()` or `asyncio.create_task()` to run the stream on a separate worker, posting messages to the Textual event loop via `self.post_message()` or `self.call_from_thread()`.
- Write a pilot test that streams 100 chunks with 10ms delays, verifying the input area remains interactive throughout.

### 6.2 Mode State Cleanup on Switch

**Risk**: Switching modes without properly cleaning up the previous mode's state leads to stale data. For example, switching from Code to Diff mode might show an outdated file change list if `ModeManager.pending_changes` is not synchronized.

**Indicators of regression**:
- Diff mode shows files that have already been accepted/rejected
- Plan mode shows a previous plan after `/clear`
- Code mode sidebar file tree retains entries from a cleared session
- Input prefix shows wrong mode tag after rapid switching

**Mitigation verification**:
- Unit tests for `ModeManager.switch()` verifying state reset/preservation semantics for each transition pair.
- Pilot test: switch through all four modes in sequence, verify sidebar, status bar, and input prefix are consistent at each step.

### 6.3 Session Persistence Format Changes

**Risk**: If the `ConversationTurn` dataclass gains or removes fields, previously saved sessions may fail to load (missing keys, extra keys, type mismatches).

**Indicators of regression**:
- `TUISession.load()` raises `KeyError` or `TypeError`
- Loaded sessions have `None` values in fields that should be populated
- New fields do not have defaults, causing old sessions to be unloadable

**Mitigation verification**:
- Schema versioning: include a `"schema_version"` field in the JSON root.
- Migration function: `TUISession.load()` detects older schema versions and applies migrations.
- Regression test: save a session with the v1 schema, add a new field, verify load still works.

### 6.4 Diff Engine Edge Cases

**Risk**: `DiffEngine.compute()` and `DiffEngine.apply_hunks()` may produce incorrect results for certain inputs: binary content, files with no trailing newline, files with only whitespace changes, very large files, or files with mixed line endings.

**Indicators of regression**:
- Applied hunks produce incorrect file content
- Binary files cause exceptions in `difflib`
- Missing trailing newline causes off-by-one in hunk line numbers
- Side-by-side view misaligns lines for whitespace-only changes

**Mitigation verification**:
- Unit tests for `DiffEngine` with these specific inputs:
  - Empty file to non-empty
  - Non-empty to empty
  - Identical files (no hunks)
  - Single character change
  - File with no trailing newline
  - File with mixed `\r\n` and `\n`
  - File containing null bytes (binary detection)
  - File with 10k+ lines (performance test: compute should complete in under 1 second)

### 6.5 File Backup/Restore Race Conditions

**Risk**: If the agent issues rapid consecutive writes to the same file, or if the user accepts a change while the agent is writing another, the backup may capture the wrong version, or the restore may overwrite a more recent accept.

**Indicators of regression**:
- Backup contains the modified version instead of the original
- Restore overwrites a user-accepted change
- Multiple backups for the same file collide (same timestamp)

**Mitigation verification**:
- `FileOps` must serialize writes to the same path (file-level lock or queue).
- Backup filenames must include sufficient timestamp precision (microseconds) or a monotonic counter.
- Integration test: issue two rapid writes to the same file, accept the first, reject the second, verify the first accepted version is on disk and the original is in backup.

### 6.6 Textual Widget Lifecycle

**Risk**: Widgets that are mounted/unmounted during mode switches (e.g. `PlanView` only exists in Plan mode, `DiffView` only in Diff mode) may not properly clean up event handlers, leading to memory leaks or ghost event handling.

**Indicators of regression**:
- Memory usage grows with each mode switch cycle
- Key bindings from a previous mode's widget fire in the current mode (e.g. `j`/`k` hunk navigation fires in Ask mode)
- Textual logs "widget not mounted" warnings

**Mitigation verification**:
- Pilot test: switch modes 100 times in a loop, verify no memory growth or warnings.
- Verify that mode-specific keybindings only fire when the corresponding mode is active.

### 6.7 Stream Chunk Ordering

**Risk**: Chunks from `ObscuraClient.stream()` arrive in a specific order (e.g. `TOOL_USE_START` before `TOOL_USE_DELTA` before `TOOL_RESULT`). If the bridge or widget does not handle out-of-order or duplicate chunks, the UI may render incorrectly.

**Indicators of regression**:
- `ToolStatus` widget shows result before start
- `ThinkingBlock` receives text after `DONE` chunk
- Duplicate `DONE` chunks cause double finalization

**Mitigation verification**:
- `BackendBridge` should validate chunk ordering or handle each chunk idempotently.
- Unit test: send chunks in various orderings, verify widgets reach correct final state.

---

## Appendix A: Test Infrastructure Requirements

| Requirement | Purpose |
|-------------|---------|
| Textual `pilot` test framework | Widget-level and integration tests without a real terminal |
| `pytest-asyncio` | Async test support for `BackendBridge` and stream tests |
| Mock `ObscuraClient` | Emits configurable `StreamChunk` sequences for deterministic testing |
| Fixture: sample session JSON files | Pre-built session files for load/migration tests |
| Fixture: sample file pairs for diff testing | Known original/modified file pairs with expected hunk outputs |
| Temporary directory fixture (`tmp_path`) | Isolated `cwd` and backup directory for `FileOps` tests |

## Appendix B: Plan-to-Codebase Gap Analysis

| Item | Plan States | Codebase Status | Gap |
|------|-------------|-----------------|-----|
| `ChunkKind.TOOL_USE_DELTA` | Used in stream chunk mapping | Exists in `sdk/_types.py` as `TOOL_USE_DELTA` | `stream_prompt()` callbacks in plan only define `on_tool_start` and `on_tool_result`; no `on_tool_delta` callback. Implementation must add one. |
| `Escape` key binding | Plan says `("escape", "cancel_stream", "Cancel")` | `sdk/cli.py` uses `Ctrl+C` for `KeyboardInterrupt` | TUI must handle both `Escape` and `Ctrl+C` for cancel. Plan bindings show `Ctrl+C` in keybindings table but `Escape` in `BINDINGS` list. Both should work. |
| `Backend` enum values | Plan supports `claude` and `copilot` | `sdk/_types.py` defines `Backend.COPILOT` and `Backend.CLAUDE` | No gap. |
| `SessionRef` usage | TUI sessions use `TUISession` with local JSON | SDK sessions use `SessionRef` with backend-managed sessions | These are separate concepts. `TUISession` manages local conversation history; `SessionRef` manages backend-side sessions. The TUI should optionally create a backend `SessionRef` for multi-turn context, and separately persist the local `TUISession`. |
| `tui` subcommand | Plan shows it added to `build_parser()` | Not yet present in `sdk/cli.py` | Must be implemented in Phase 1. |
| `system_prompt` per mode | Plan defines mode-specific system prompts in `ModeManager.get_system_prompt()` | `ObscuraClient.__init__` accepts `system_prompt: str` | System prompt must be updated on mode switch. Either create a new `ObscuraClient` per mode switch, or find a way to update the system prompt on the existing client/backend. This needs design resolution. |

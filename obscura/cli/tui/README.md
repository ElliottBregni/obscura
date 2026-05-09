# obscura.cli.tui

## Overview

`obscura tui` is a full-screen prompt-toolkit front-end for the same agent engine that powers the bordered REPL. Launch it with `uv run obscura tui` (plus the usual `-b/--backend`, `-m/--model`, `--session`, `--workspace` flags). It is a sibling of the legacy bordered REPL — not a replacement; both surfaces live side by side and share the engine, widget kit, and renderer protocol.

## When to use which

The bordered REPL (default `obscura`) is better for piping, scripting, dumb terminals, and CI; the full-screen TUI (`obscura tui`) is better for live agent fleets, long-running sessions where a sticky input box, transcript scrollback, and modal approvals matter.

## Architecture diagram

```
Click `obscura tui`
  -> run_tui(cfg)
    -> bootstrap_env()
    -> bootstrap_tui_session(cfg)  --->  build_repl_session  --->  AgentSession
    -> ObscuraTUIApp(handle)
        |-- TUIState (Pydantic, mutated in place)
        |-- TUIRenderer (impl RendererProtocol)
        |     ^ from_agent_event() routes Agent events to channels
        |-- build_layout(state)   -> header, transcript, live, notifications, banner, input, toolbar
        |-- build_overlays(state) -> tool_approval, command_palette, ask_user, plan_approval
        |-- prompt_toolkit.Application
        \-- async background tasks (spinner tick, notification prune)
```

## Module map

| File | Purpose | Public API |
| --- | --- | --- |
| `__init__.py` | Package facade re-exporting state types and `run_tui`. | `run_tui`, all `TUIState`-family types |
| `state.py` | Pydantic state container + enums + mutators. | `TUIState`, `HUDState`, `LiveRegionState`, `TranscriptEntry`, `NotificationItem`, `ToolApprovalRequest`, `make_tui_id` |
| `engine_adapter.py` | Bootstraps the shared engine with TUI host callbacks. | `TUIEngineConfig`, `TUIEngineHandle`, `bootstrap_tui_session`, `stream_one_turn` |
| `renderer.py` | `RendererProtocol` impl that mutates `TUIState`. | `TUIRenderer` |
| `formatter.py` | Pure event-to-state primitive translators (no side effects). | `format_transcript_event`, `format_status_event`, `format_notification`, `format_banner`, `format_user_prompt`, `format_slash_output` |
| `buffers.py` | Pure `FormattedText` factories read from `TUIState`. | `header_text`, `banner_text`, `transcript_text`, `notification_stack_text`, `live_region_text`, `toolbar_text` |
| `layout.py` | Builds the full-screen `Layout`, returns `TUILayoutComponents`. | `build_layout`, `TUILayoutComponents` |
| `overlays.py` | Modal `Float` overlays bound to state. | `ToolApprovalOverlay`, `CommandPaletteOverlay`, `AskUserOverlay`, `PlanApprovalOverlay`, `TUIOverlays`, `build_overlays` |
| `app.py` | Top-level `Application` glue, slash dispatch, background tasks. | `ObscuraTUIApp` |
| `runtime.py` | Public entry point invoked by the Click subcommand. | `run_tui` |

## State model

- `TUIState` — top container; holds everything else and exposes mutators.
- `HUDState` — backend, model, session id, branch, workspace, mode, permission mode, context %, running agents, task count, streaming flag.
- `TranscriptEntry` — one committed scrollback block; `kind: TranscriptKind`, ordered `runs: list[StyledRun]`, `metadata`, `parent_id` (for tool pairs), `collapsed`.
- `LiveRegionState` — ephemeral spinner row (`kind`, `label`, `preview`, `spinner_idx`, `started_at_monotonic`, `elapsed_s`).
- `NotificationItem` — toast (id, severity, title/body, source, key for replace-by-key, ttl).
- `BannerState` — sticky callout (`kind` literal: plan_approval / capability_denial / arbiter_kill / compaction).
- `ToolApprovalRequest` — frozen request consumed by `ToolApprovalOverlay`; carries `risk` and `preview` fields ready for richer previews.
- `RunningAgentSnapshot`, `StyledRun` — toolbar + styled-tuple primitives.

Mutators on `TUIState`: `append_transcript` (caps to 5000), `push_notification` (replaces by `key`, caps to 12), `prune_notifications` (TTL sweep), `clear_banner`, `open_approval`, `close_approval`.

## Event flow

1. User types `text` into the bottom `TextArea`.
2. The `Buffer` accept handler calls the layout's `_accept`, which calls `app._on_submit_sync(text)`.
3. `_on_submit_sync` clears the buffer and schedules `app.submit_user_input(text)` on the running event loop.
4. `submit_user_input` strips/branches: a `/`-prefixed line goes to `_dispatch_slash_command`; everything else to `_stream_prompt`.
5. `_stream_prompt` calls `renderer.push_user_prompt(text)`, then iterates `handle.submit(text)` (= `session.stream_loop`).
6. Each `AgentEvent` is fed to `renderer.handle(event)`, which routes through `obscura.cli.renderer.channels.from_agent_event` and dispatches to per-channel state mutators (transcript / status / notification / banner).
7. After every event the renderer fires the `invalidate` callback; the next prompt-toolkit frame re-reads buffers from `TUIState` and redraws.

## Slash commands

A submitted line starting with `/` is captured before the engine sees it. `_dispatch_slash_command` builds a minimal `REPLContext` (live `AgentSession`, lazy event store, current backend/model/system_prompt/max_turns/tools_enabled), redirects `obscura.cli.render.console.file` to a `StringIO`, and calls `obscura.cli.commands.handle_command(line, ctx)`. The captured Rich output is fed through `format_slash_output` and appended as a `TranscriptKind.SLASH_OUTPUT` entry. Returning `"quit"` exits the application.

`COMPLETIONS` from `obscura.cli.commands` drives both the `SlashCommandCompleter` on the input box and the command palette. The full set covers session, agent, KAIROS, plugin, debug, vector memory, eval, workflow, git, and advanced categories.

## Overlays

- `ToolApprovalOverlay` — opened when a tool gate sets `state.pending_approval`; shows tool name, args (pretty JSON, capped 20 lines), optional preview, and a hotkey row. Border colour reflects `request.risk`.
- `CommandPaletteOverlay` — opened on `Ctrl-K`; filterable list of slash commands, navigated with Up/Down, selected with Enter, dismissed with Esc.
- `AskUserOverlay` — opened by `ask_user_callback` host callback; one-line text input with a wrapped prompt above. Enter submits, Esc cancels.
- `PlanApprovalOverlay` — opened by `plan_approval_callback`; sticky banner-like float with summary body and approve/reject hotkeys.

## Key bindings

| Binding | Scope | Action |
| --- | --- | --- |
| `Ctrl-D` | App | Exit cleanly with code 0 |
| `Ctrl-C` | App | Cancel the in-flight stream (via `cancel_event`); does not exit |
| `Ctrl-K` | App | Open the command palette overlay |
| `F1` | App | Push a hotkeys cheat-sheet notification |
| `F2` | App | Toggle the running-agents tree in the toolbar |
| `Ctrl-T` | Input | Expand last thinking block (via legacy `_expand_thinking_action`) |
| `Ctrl-P` | Input | Expand the latest preview as Markdown |
| `Ctrl-Space` | Input | Insert the `__VOICE_RECORD__` sentinel and submit |
| `Esc + Enter` | Input | Insert a newline (multi-line input) |
| `Up` / `Down` | Palette | Navigate filtered command list |
| `Enter` | Palette / AskUser | Select / submit |
| `Esc` | Palette / AskUser / Approvals | Cancel / deny |
| `y` / `n` / `a` | Tool approval | Allow / deny / always-allow |
| `y` / `n` | Plan approval | Approve / reject |

## Reusing existing infrastructure

The TUI does not reimplement features. It reuses:

- `obscura.composition.repl.build_repl_session` — engine + supervisor + KAIROS + iMessage daemon + browser bridge + plugin loading + memory channels + project hooks + session registration.
- `obscura.cli.commands.handle_command` and `COMPLETIONS` — slash command dispatch and completer dataset.
- `obscura.cli.promptkit.*` — `SlashCommandCompleter`, `_make_key_bindings`, `KeywordHighlighter`, `PROMPT_STYLE`.
- `obscura.cli.renderer.channels.from_agent_event` — single source of truth for routing `AgentEvent` to a channel.
- `obscura.cli.renderer.modern.theme` — Catppuccin Mocha palette for buffers and overlays.
- `obscura.cli.widgets` — UX patterns the overlays follow (the TUI floats are visually consistent with the bordered REPL panels).
- `obscura.voice/`, `obscura.auth/`, `obscura.core.tool_context` — unchanged; the TUI binds `host_callbacks` so any tool reading `current_tool_context()` reaches the overlays.

## Adding a new overlay

1. Create a class in `overlays.py` with `visible: bool`, `float: Float`, `key_bindings: KeyBindings`, and an async `request(...)` method that opens the float, awaits a `Future`, clears state, returns the result.
2. Add it to the `TUIOverlays` dataclass and instantiate it inside `build_overlays`.
3. Route a field on `TUIState` (or one of its sub-models) that drives the visibility filter.
4. Wire the overlay's `request` method as a callback in `ObscuraTUIApp._wire_overlay_callbacks`, both onto `self._handle.<cb>` and onto `self._handle.session.host_callbacks`.
5. If the overlay gates a tool, ensure the corresponding key (e.g. `ask_user_callback`) is the one composition blocks read from `host_callbacks`; tools then reach the overlay through `current_tool_context()`.

## Tests

- `tests/unit/obscura/cli/tui/` — unit coverage of state mutators, formatter purity, buffer factories, renderer channel dispatch, and overlay lifecycle.
- `tests/integration/cli/test_tui_smoke.py` — end-to-end smoke that boots `ObscuraTUIApp` against a stub engine handle and runs one turn through the layout.

## Known limitations / where to look next

The TUI inherits a handful of legacy code paths that work today but are flagged for cleanup. See [`DEFERRED_REWRITES.md`](DEFERRED_REWRITES.md) for the concrete sub-plans.

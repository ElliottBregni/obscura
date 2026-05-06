# TUI deferred rewrites

Features the full-screen TUI uses today but inherits from legacy code paths that should be cleaned up. Each entry is a self-contained sub-plan that can be picked up later without re-reading the original ticket.

### Pre-execution risk preview for tool approval
- **Status:** `ToolApprovalOverlay` shows tool args as pretty JSON only; no diff, no dry-run, no command preview. `ToolApprovalRequest.preview` and `ToolApprovalRequest.risk` already exist on the model but are populated only when the caller pre-computes them.
- **Why deferred:** Building a per-tool previewer registry is a separable, lower-priority polish item — the modal already enforces approval correctly without it.
- **Plan to revisit:** Add a `compute_preview(tool_name: str, tool_input: dict) -> str` registry under `obscura/cli/tui/` (or co-located with the tools), wire it into wherever `open_approval` is called, and have the registry default to "" when no previewer is registered. Start with the highest-risk tools: `bash`, `edit_text_file`, `write_text_file`, `apply_patch`.
- **Owner / next step:** `obscura/cli/tui/state.py:ToolApprovalRequest`.

### `commands.py` monolith -> `CommandResult`
- **Status:** Slash commands print directly via the Rich `console`. The TUI captures this through a stdout redirect (`_dispatch_slash_command` swaps `rich_console.file`); it works but loses structure — colours come back as ANSI bytes that we strip before display.
- **Why deferred:** The capture path is correct enough for v1; refactoring all ~70 commands is mechanical but large.
- **Plan to revisit:** Define a typed `CommandResult` (`entries: list[StyledRun]`, `exit_code: int`, optional structured fields per command). Refactor each command to return `CommandResult` instead of printing. Update both surfaces to consume the result. Start with high-traffic commands (`/help`, `/session`, `/diff`, `/agent`).
- **Owner / next step:** `obscura/cli/commands.py`.

### `render.py` formatter/emitter split
- **Status:** Legacy `StreamRenderer` mixes formatting (deciding what to show) and stdout emission (writing to the terminal). The new TUI formatter (`obscura/cli/tui/formatter.py`) is the clean separation — pure functions producing `TranscriptEntry` / `LiveRegionState` / `NotificationItem` / `BannerState`.
- **Why deferred:** The legacy `StreamRenderer` still works for the bordered REPL; both surfaces can ship with their own renderer for now.
- **Plan to revisit:** Port the bordered-REPL `StreamRenderer` to consume `obscura.cli.tui.formatter`, deleting its inline formatting logic. The emitter half stays in `render.py` and only knows how to translate `TranscriptEntry` -> Rich panels.
- **Owner / next step:** `obscura/cli/render.py`.

### `tui_effects.py` ultrathink banner
- **Status:** The "ultrathink" banner currently writes directly to stdout, bypassing the TUI's buffers entirely (it pretends the screen is plain).
- **Why deferred:** The effect is cosmetic and rarely fires; getting it right is downstream of having a proper live-region streaming abstraction.
- **Plan to revisit:** Rewrite the effect as an async generator yielding `StyledRun` frames over time. The TUI runs it as a background task feeding `state.live` (or a dedicated effect slot). The bordered REPL keeps the stdout path.
- **Owner / next step:** `obscura/cli/tui_effects.py`.

### `patch_stdout` reliance in legacy widgets
- **Status:** `confirm_prompt_async` and friends in `obscura/cli/widgets.py` use prompt-toolkit's `patch_stdout`. That is fine for the bordered REPL but conflicts with a full-screen `Application`, which owns the entire screen.
- **Why deferred:** The TUI does not call these widgets in its happy path — overlays handle ask-user / confirm cases. They are only a risk if a stray composition block invokes them while the TUI app is running.
- **Plan to revisit:** Route every `confirm`/`prompt` through `engine_adapter`'s `host_callbacks` so the TUI overlays handle them and the bordered REPL keeps the legacy widget. Audit `obscura/cli/widgets.py` callers and migrate.
- **Owner / next step:** `obscura/cli/widgets.py`.

### Renderer module-level `_active_renderer` global
- **Status:** `obscura/cli/render.py:set_active_renderer` is a module-level singleton. The TUI app sets it at startup so `Ctrl-P` / `Ctrl-T` (legacy `_expand_preview_action` / `_expand_thinking_action`) reach the live renderer.
- **Why deferred:** A v1 wart — works because there is one TUI per process, but it tangles renderer state with the import graph.
- **Plan to revisit:** Move expand-preview / expand-thinking hotkeys to read from a session-scoped registry (e.g. via `current_tool_context()` or a `ContextVar`) so multiple concurrent agents in one process do not clobber each other's renderers.
- **Owner / next step:** `obscura/cli/render.py:set_active_renderer`.

### Voice input wiring
- **Status:** `Ctrl-Space` inserts the `__VOICE_RECORD__` sentinel string into the input buffer; the bordered REPL's input loop intercepts it. The TUI inherits the same key binding via `_make_key_bindings` and would have to grow a parallel intercept.
- **Why deferred:** Voice is gated by `OBSCURA_*` env vars and is not on the critical path for the TUI ship.
- **Plan to revisit:** Extract `obscura.voice.session.VoiceSession` invocation into a thin `VoiceProvider` adapter the app holds. Replace the sentinel with a direct key binding that calls `provider.record_async()`, splices the transcript into the buffer, and submits.
- **Owner / next step:** `obscura/cli/promptkit/keybindings.py:_voice_record`.

### Slash-command output ANSI handling
- **Status:** `format_slash_output` strips ANSI CSI/OSC sequences before emitting a single plain `StyledRun`. Colour from Rich is lost.
- **Why deferred:** Colour is nice-to-have; correctness (no escape bleed into the buffer) is what matters for v1.
- **Plan to revisit:** Write a small ANSI -> `StyledRun` parser (CSI SGR only is enough) and emit one `StyledRun` per attribute run instead of stripping. Keep the strip-fallback for malformed input.
- **Owner / next step:** `obscura/cli/tui/formatter.py:format_slash_output`.

### Mode toggling (`/mode plan`)
- **Status:** The TUI's `permission_mode_cb` is async (it pushes a notification and updates `HUDState.permission_mode`), but the legacy mode-change code in `_repl_loop.py` is sync.
- **Why deferred:** The async path works for the TUI; the sync path works for the bordered REPL. Aligning them is mechanical.
- **Plan to revisit:** Make the callback signature optional + sync when called from the bordered REPL surface (e.g. `Callable[[str], Awaitable[None] | None]` + a small adapter), or split into two slots. Either way, document the contract on `engine_adapter.TUIPermissionModeCallback`.
- **Owner / next step:** `obscura/cli/tui/engine_adapter.py:TUIPermissionModeCallback`.

---

To add an entry to this doc, paste the four-line schema (`Status`, `Why deferred`, `Plan to revisit`, `Owner / next step`) under a new `### <feature>` heading and link the closest concrete file or symbol so the next reader can start without re-discovering the context.

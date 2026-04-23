# Codex MCP parity — implementation plan

**Status:** drafted; hold pending commit of current working-tree changes.
**Delivery target:** single PR, built inside a `git worktree` so the main repo
stays on `main` during implementation.

## Problem

The Codex backend exposes `browser_*` tools in its *developer-instructions*
prompt (via `register_tool()`) but has no execution wire for them — the
Codex SDK runs a closed tool loop that only calls tools it discovered
through its own `mcp_servers` list. Result: the model names the tool in
chat output, then gives the user a paste-this-snippet instead of calling
it. Claude and Copilot don't have this gap because their backends
dispatch tool calls through Obscura's own tool executor.

We also have a latent bug: `CodexBackend.__init__` stores `_mcp_servers`
but `_build_thread_start_kwargs()` never forwards it to `thread_start`,
so any user-configured MCP servers for Codex are silently dropped today.

## Goals

1. **Codex can call `browser_*` tools at the same latency and reliability
   as Claude/Copilot.** A user saying *"make this page green"* results in
   a real `browser_eval_js` call, not a paste snippet.
2. **Claude and Copilot behavior is byte-identical** after the change.
   Every modification is either additive or gated on `backend == codex`.
3. **Fix the pre-existing `_mcp_servers` drop.** Any MCP server declared
   for a Codex session (browser tools or otherwise) actually reaches the
   SDK.
4. **Single tool surface per backend.** Codex sees browser tools *only*
   via MCP (not inlined in the developer-instructions prompt). Claude and
   Copilot see them *only* via `register_tool()`. No double-registration.
5. **Stretch goal (decide before starting):** the in-process MCP server
   optionally exposes *all* Obscura-registered tools, not just the
   browser 17, so Codex reaches tool parity with the other backends
   universally. Default: yes, include; see § Open questions.

## Non-goals

- No Tampermonkey changes. Covered in previous work.
- No refactor of `register_tool()` or Obscura's tool executor. Claude and
  Copilot stay on their current path.
- No change to the browser extension's JS or manifest.
- No migration of CLI/REPL Codex behavior — CLI sessions don't currently
  need browser tools and we don't want to accidentally spin up the SSE
  server in non-browser contexts. Gated by the native host.
- No Node.js daemon port. Keep-native-messaging-add-more-tools per the
  turning-point note in earlier conversation.

## Design

### In-process FastMCP server inside the native host

The native host (`packages/browser-extension/native-host/obscura_native_host.py`)
becomes the MCP host process. It starts a FastMCP SSE server on
`127.0.0.1:<ephemeral-port>` lazily — only the first time a Codex session
is created in that panel's lifetime.

```
native host process
├── stdio native-messaging loop        (existing; extension ↔ host)
├── ObscuraSession                     (existing; Claude/Copilot/Codex)
└── NEW  FastMCP SSE server on 127.0.0.1:N/sse
          wraps browser_tools.TOOLS (+ optionally all session tools)
          handlers call browser_tools._call() which frames back to the panel
```

**Transport choice: SSE over ephemeral-port HTTP**, not stdio:
- Tools must stay in the host's address space because they close over
  `_write_frame` (the stdin/stdout for Chrome's native messaging). A
  stdio-MCP subprocess can't reach that.
- SSE is what `modelcontextprotocol` clients accept as a URL server; the
  existing `mcp` Python SDK (`mcp.server.fastmcp.FastMCP`,
  `mcp.server.sse.SseServerTransport`) is already an installed dep — see
  `obscura/integrations/mcp/server.py` which uses it.
- Ephemeral port (`bind('127.0.0.1', 0)`) avoids collisions with user
  services and other Chrome profiles running Obscura.
- 127.0.0.1 only. No cross-origin risk.

### Per-backend tool-registration split

In `obscura_native_host.py`'s `_ensure_session()`, after creating the
session, fork on backend:

```python
if backend == "codex":
    # Start (or reuse) the in-process browser MCP server.
    url = _ensure_browser_mcp_server()
    session.client._backend._mcp_servers.append(
        {"name": "obscura-browser", "url": url}
    )
    # Intentionally DO NOT call register_tool for browser tools on codex —
    # they live on the MCP surface instead. Avoids Codex seeing the tool
    # inlined in its developer-instructions prompt AND via MCP discovery.
else:
    for tool in _ensure_browser_tools():
        session.client.register_tool(tool)
```

### Wiring `_mcp_servers` into Codex

`obscura/providers/codex.py:_build_thread_start_kwargs` currently skips
`self._mcp_servers`. Add one block (exact shape depends on what the
installed `codex_app_server` SDK exposes — determined at runtime via
getattr probing, same pattern as `AskForApproval`/`SandboxMode`):

```python
if self._mcp_servers:
    # Pre-existing gap: _mcp_servers was stored and never passed through.
    kwargs["mcp_servers"] = list(self._mcp_servers)
```

If the SDK wants a typed class (e.g. `MCPServerConfig`), probe
`self._sdk_syms` the same way we probe `AskForApproval`. Fall back to
raw dicts. **Before writing code: confirm the SDK's expected shape by
`uv run python -c "from codex_app_server import ...; help(...)"`.**

### Running the MCP server inside the host's asyncio loop

The existing native host `_main()` is already an asyncio coroutine. We
add:

```python
_browser_mcp_url: str | None = None

async def _ensure_browser_mcp_server() -> str:
    global _browser_mcp_url
    if _browser_mcp_url is not None:
        return _browser_mcp_url
    from browser_mcp_server import start_browser_mcp
    _browser_mcp_url = await start_browser_mcp()
    return _browser_mcp_url
```

The server runs as a background task inside `_main()`'s loop; no new
thread. Shutdown is handled on host exit (the task is cancelled in the
existing finally block).

### `browser_mcp_server.py`

New file alongside `browser_tools.py`. Exposes `start_browser_mcp() ->
str` returning the `http://127.0.0.1:N/sse` URL. Uses `FastMCP` to
register each entry of `browser_tools.TOOLS` with a thin handler that
delegates to `browser_tools._call(op, args)`.

Pseudocode:

```python
from mcp.server.fastmcp import FastMCP
import browser_tools

async def start_browser_mcp() -> str:
    mcp = FastMCP("obscura-browser")
    for spec in browser_tools.TOOLS:
        def make_handler(op_name, schema):
            async def handler(**kwargs):
                return await browser_tools._call(op_name, kwargs)
            return handler
        mcp.add_tool(
            fn=make_handler(spec.name.removeprefix("browser_"), spec.parameters),
            name=spec.name,
            description=spec.description,
        )
    # Bind to ephemeral port, return the SSE URL.
    host, port = "127.0.0.1", 0  # let OS pick
    # Use mcp.run_sse_async or the equivalent mount on aiohttp/starlette.
    ...
    return f"http://127.0.0.1:{actual_port}/sse"
```

Decision point during implementation: `FastMCP.run_sse_async()` vs.
mounting on a Starlette app and controlling the port manually. The
latter is how `obscura/integrations/mcp/server.py` does it — follow that
precedent.

## File-by-file changes

| File | Change | Approx. lines |
|------|--------|---------------|
| `obscura/providers/codex.py` | In `_build_thread_start_kwargs`, forward `self._mcp_servers` to `thread_start`. Probe SDK symbols for a config class if needed. | ~15 |
| `packages/browser-extension/native-host/browser_mcp_server.py` | **NEW.** FastMCP SSE server re-exporting `browser_tools.TOOLS`. Exposes `start_browser_mcp() -> url`. | ~120 |
| `packages/browser-extension/native-host/obscura_native_host.py` | In the session setup, fork on `backend == "codex"`: start MCP server, inject URL into `session.client._backend._mcp_servers`, skip `register_tool`. Else: existing path. | ~25 |
| `tests/unit/obscura/providers/test_backend_codex.py` | Add test: when `mcp_servers=[...]` is passed to `CodexBackend`, `_build_thread_start_kwargs` includes it. | ~20 |
| `tests/browser_extension/test_browser_mcp_server.py` | **NEW.** Smoke test: `start_browser_mcp()` returns a reachable SSE URL; listing tools returns the 17 browser specs. | ~40 |
| `tests/browser_extension/test_host_lifecycle.py` | Extend: assert no MCP server is spawned when the session's backend is copilot. | ~15 |

Total: ~235 lines net-new, spread over ~6 files. No deletions of
existing behavior.

## Execution order (step-by-step)

1. `git worktree add ../obscura-codex-mcp codex-mcp-parity` from the
   main repo root. New branch from `main`.
2. Confirm the `codex_app_server` SDK's expected `mcp_servers` shape
   (introspect or read the installed package source).
3. Write `tests/unit/obscura/providers/test_backend_codex.py` for the
   new kwarg pass-through. **Fail first.** Land the codex.py change to
   make it pass.
4. Write `browser_mcp_server.py` with a direct call-path (no HTTP) so
   the logic is testable: a `list_tools()` + `call_tool(name, args)`
   pair that exercises `browser_tools._call()` via a fake write_frame.
5. Add the SSE server wrapper around it. Confirm that `curl -N
   http://127.0.0.1:N/sse` returns an MCP handshake.
6. Wire the native host to start the server on first Codex session and
   skip `register_tool` for browser tools.
7. Smoke test end-to-end: open the panel on Codex, ask *"list open tabs"*,
   verify a real `browser_list_tabs` call lands (log shows a
   `browser-tool` frame out → `browser-tool-response` in).
8. Run `ruff check`, `ruff format`, `pyright --strict`, `pytest
   tests/browser_extension tests/unit/obscura/providers`.
9. Squash, push, open PR.

## Tests

Running list. Each should be added before the corresponding code
change where feasible.

- **Unit: `test_backend_codex.py`**
  - `mcp_servers` kwarg reaches `thread_start` when non-empty.
  - Empty list does not set the kwarg (no behavior change for existing
    sessions).
- **Unit: `test_browser_mcp_server.py`**
  - `start_browser_mcp()` returns a URL that starts with `http://127.0.0.1:`.
  - MCP `list_tools` returns exactly `len(browser_tools.TOOLS)` entries
    with matching names.
  - MCP `call_tool("browser_list_tabs", {})` round-trips through a
    stubbed `_write_frame` (fake browser-tool-response via
    `browser_tools.resolve(...)`).
- **Integration: `test_host_lifecycle.py`**
  - When `backend="copilot"`, no MCP server is started (ensure
    `_browser_mcp_url` stays `None`).
  - When `backend="codex"`, the MCP server is started and its URL
    appears in the backend's `_mcp_servers`.

## Manual verification

Run these from the reloaded extension on a test tab (GitHub or any
non-trivial page):

1. `/backend codex`
2. *"Open a new tab to news.ycombinator.com"* → transcript shows a
   `browser_new_tab` tool call bubble, new tab appears.
3. *"What's the first headline?"* → `browser_read_page` call, response
   uses the real page text.
4. *"Make the page green"* → `browser_eval_js` call, page turns green
   before the reply text finishes streaming.
5. `/backend copilot` → same three prompts. Should behave identically
   to today (no regression).
6. `/backend claude` → same. No regression.

Pass criteria: every Codex prompt that previously returned a paste
snippet now emits a tool call bubble with `browser_*` and the
corresponding side effect in the active tab.

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Codex SDK rejects our `mcp_servers` shape. | Probe the SDK for a typed class at import time (same pattern as `AskForApproval`); fall back to dict; log an actionable error if neither works. |
| Port-binding failure in restrictive environments. | Try `127.0.0.1:0`; if bind fails, log a warning and fall back to the current behavior (browser tools unavailable on Codex; user sees the old paste-snippet flow rather than a crash). |
| Server leaks threads/sockets across host reloads. | Tie server lifetime to the host's asyncio loop; cancel on shutdown. Add a finally block in `_main()`. |
| Double-exposing a tool (MCP + developer-instructions). | The Codex branch of the native host setup explicitly skips `register_tool` for browser tools, so the system prompt doesn't list them on Codex. Confirmed by inspecting `CodexBackend._build_tool_listing` — only `self._tools` entries appear. |
| Claude/Copilot regression. | Every change is gated on `backend == "codex"` or on a new file. A diff review of the non-gated lines should show only the `_mcp_servers` kwarg addition in `codex.py`, which is inert when the list is empty. |
| Long cold-start on first Codex turn. | Start the SSE server at host boot (not on first Codex turn) so the port is ready when a backend switch happens. Cheap — single Starlette app on an ephemeral port. |
| Cross-profile collisions (two Chrome profiles). | Each native host is its own process; each gets its own port. No shared state. |

## Rollback

Entirely contained in the worktree branch. Revert = `git revert` the
merge commit or delete the branch before merging. No data migrations,
no config files, no manifest changes.

## Open questions (resolve before starting)

1. **Scope of the MCP server — resolved:** browser tools only for v1.
   Rationale: Codex has no documented hard cap on MCP tools, but every
   tool schema serializes into the prompt and GPT-5's tool-selection
   accuracy degrades past ~30–40 tools. Obscura has ~100+ tools plus
   plugin-contributed ones; piping them all in would hurt Codex
   selection accuracy without adding much value (Codex already has its
   own shell/file/web tools). For v2, expose a *single* `search_tools`
   MCP entry that queries `session.client._tool_registry.all()` by
   keyword — same affordance `/search-tools` gives humans. Don't expand
   the flat list.
2. **Should the MCP server run in CLI Codex sessions too?** No for v1 —
   CLI has no browser tools to expose and no native host. If/when we
   want CLI Codex parity for non-browser tools, move the MCP server
   into Obscura proper, gated on backend.
3. **Server binding: SSE vs streamable-http?** Streamable-HTTP is the
   newer MCP transport (single endpoint, supports POST+GET). SSE is
   what Codex's own docs name. Confirm which the installed SDK
   supports. Prefer SSE for v1 because the existing
   `obscura/integrations/mcp/server.py` already uses it.

## Timeline estimate

3–5 hours of focused work. Rough breakdown:

- SDK introspection + `codex.py` fix + test: 30 min
- `browser_mcp_server.py` + unit tests: 2 hours
- Native host wiring + integration tests: 1 hour
- End-to-end manual verification + polish: 30-60 min

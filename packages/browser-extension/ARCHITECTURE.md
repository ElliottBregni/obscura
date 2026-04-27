# Architecture

How the Obscura browser extension plugs into the main obscura codebase
without forking it. Read this before making non-trivial changes.

## One-sentence description

**The extension is a Chrome MV3 side panel whose service worker proxies
messages over stdio to a local Python "native messaging host", which
drives a real `ObscuraSession` and streams its `AgentEvent`s back as
wire frames.**

Everything else is a consequence of that sentence.

---

## Topology

```
                        ┌────────────────────────────┐
                        │   Side panel (HTML/JS)     │  port "sidepanel"
                        │   src/sidepanel/*.js       │
                        └────────────┬───────────────┘
                                     │ chrome.runtime.connect
                                     ▼
                        ┌────────────────────────────┐
                        │   Service worker           │  bridge
                        │   src/background.js        │
                        └────────────┬───────────────┘
                                     │ chrome.runtime.connectNative
                                     │ "com.obscura.host"
                                     │
                      length-prefixed JSON (4-byte LE + body)
                                     │
                                     ▼
                        ┌────────────────────────────┐
                        │   Native messaging host    │
                        │   native-host/             │
                        │     obscura_native_host.py │
                        │     browser_tools.py       │
                        └────────────┬───────────────┘
                                     │ monkey-patched hooks
                                     ▼
                        ┌────────────────────────────┐
                        │   ObscuraSession           │
                        │   obscura.cli.session      │  (unchanged core)
                        └────────────────────────────┘
```

Three processes, three protocols:

| Hop | Transport | Framing |
|-----|-----------|---------|
| panel ↔ SW | `chrome.runtime.Port` | JS objects (structured clone) |
| SW ↔ host | `chrome.runtime.connectNative` | 4-byte LE length + UTF-8 JSON |
| host ↔ obscura | direct Python calls | `AgentEvent` / `ToolCall` |

The panel cannot speak directly to the host — Chrome's native-messaging
API only exposes it to the service worker, which is why `background.js`
exists as a pure forwarder.

---

## Why a "thin adapter" matters

The first prototype forked the REPL's send/receive pipeline, memory
search, $skill parsing, auto-compact, etc. It drifted within days.

The current design installs **three monkey-patches** on extension points
obscura already ships, then calls `ObscuraSession.create()` +
`session.send()` like any other consumer:

1. **`obscura.cli.renderer.create_renderer`** → returns our
   `BrowserRenderer`. Every `AgentEvent` becomes a wire frame. See
   `_install_renderer_factory()` in the host.
2. **`obscura.cli.widgets.confirm_*` + `confirm_prompt_async`** → routed
   through `widget` frames, resolved by `widget-response`. See
   `_install_widget_broker()`.
3. **`obscura.cli.render.console.file`** → rebound to a streaming
   writer so `console.print(...)` in any `cmd_*` emits live `chunk`
   frames. See `_install_console_proxy()`.

Browser-specific tools (`browser_read_page`, `browser_click`, etc.) are
normal `ToolSpec`s registered via the *public*
`session.client.register_tool()` API.

**The rule:** if a feature can be expressed by patching an existing
obscura extension point or registering a tool, do that. Don't fork.

---

## Wire protocol

All frames are `dict` bodies. Every send-style frame has an `id` for
multiplexing; responses echo the same `id`.

### ext → host

| `type` | Payload | Notes |
|--------|---------|-------|
| `send` | `{id, prompt, backend, model?, workspace?, session_id?, context}` | Regular chat turn |
| `command` | `{id, raw, backend}` | Slash command (`/foo`) |
| `cancel` | `{target_id}` | Aborts in-flight send/command |
| `kairos` | `{action: "on"|"off"}` | Toggle daemon |
| `diag` | `{id}` | Request diagnostics snapshot |
| `list_sessions` | `{id}` | Enumerate saved sessions |
| `browser-tool-response` | `{id, ok, result?, error?}` | Reply from DOM tool execution |
| `widget-response` | `{widget_id, action, text?}` | Reply from confirm/attention dialog |
| `shutdown` | — | Graceful exit |
| `ping` | `{id}` | Keepalive / spawn-trigger |

### host → ext

| `type` | Payload | Notes |
|--------|---------|-------|
| `ready` | `{version, python, git_commit, backends, commands, skills, at_commands, workspaces, pid}` | Once on connect |
| `chunk` | `{id, text}` | Assistant text delta |
| `thinking` | `{id, text}` | Reasoning delta (collapsible in UI) |
| `tool_start` / `tool_delta` / `tool_end` | `{id, tool_use_id, tool_name?, delta?}` | Tool-call lifecycle |
| `tool_result` | `{id, tool_use_id, text, is_error}` | Result of a tool |
| `resolved` | `{id, tokens: ["$x", "@y"]}` | Skill/command breadcrumb |
| `widget` | `{id (widget_id), kind, question, actions, default?, detail?}` | Confirmation / attention / question dialog |
| `browser-tool` | `{id, op, args}` | Request to run DOM op in the panel |
| `kairos` | `{state}` | Daemon state change |
| `fleet` | `{event, agent?, status?, timestamp, detail?, agents?}` | Live fleet observability — `event` ∈ `snapshot` / `agent_started` / `agent_stopped` / `agent_error` / `agent_activity`. `snapshot` carries `agents: [{agent, status, lastActivity, lastError, model}]`; per-agent events carry `agent` + `status` (`running`/`idle`/`error`/`stopped`). Emitted by the host's fleet observer (lifecycle hook on every `AgentRuntime` in-process + 2 s polling diff). Silent when no runtime is active — panel falls back to `/agent list`. |
| `auth_required` | `{id, message}` | Auth gate challenge |
| `done` | `{id, session_id, text_len}` | Turn finished |
| `error` | `{id?, message, trace?}` | Any failure |
| `pong` | `{id}` | Keepalive reply |

Adding a new frame type: update the match in `background.js`'s
forwarder (usually zero-change — it just forwards), then handle the
frame in both `_main()` of the host and the `port.onMessage` switch in
`sidepanel.js`. **Always update this table and the protocol docstring
at the top of `obscura_native_host.py` in the same PR.**

---

## Process lifecycle

1. User clicks the toolbar icon → Chrome opens the side panel.
2. `sidepanel.js` runs `connect()` → creates a port named `"sidepanel"`.
3. Service worker's `onConnect` handler accepts it and sends
   `{type: "bridge-ready"}`.
4. Panel sends `{type: "ping", id: "boot"}` to kick the SW to spawn the
   native host (Chrome only spawns on first outgoing message).
5. SW calls `chrome.runtime.connectNative("com.obscura.host")`. Chrome
   reads the manifest at
   `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.obscura.host.json`,
   which points at `native-host/obscura-native-host` (the shell
   launcher generated by `install.sh`).
6. The launcher resolves `OBSCURA_PYTHON`, sources
   `~/.obscura/browser-host.env`, auto-pulls `GH_TOKEN` from `gh auth
   token` if unset, and `exec`s `python obscura_native_host.py`.
7. The host installs widget broker + renderer factory + console proxy,
   writes the `ready` frame, then enters its read loop.
8. Subsequent panel messages are forwarded SW → host verbatim. Host
   responses are forwarded SW → all open panels via `broadcastToPanels`.
9. On panel close / reload, the port disconnects. Host stays alive
   until explicit `shutdown` or Chrome kills it (Chrome keeps hosts
   alive for the life of the extension by default).

**Gotcha:** editing the host or launcher has no effect until the host
process is killed. Use `make ext-reload` or `obscura-browser reload`.

---

## Data flow for a `send`

```
panel               background.js        host                     ObscuraSession
  │                     │                  │                          │
  │ send(id,prompt)     │                  │                          │
  ├────────────────────►│                  │                          │
  │                     │ send(...)        │                          │
  │                     ├─────────────────►│                          │
  │                     │                  │ _ensure_session()        │
  │                     │                  │ ├───►lazy create─────────►│
  │                     │                  │ session.send(prompt)     │
  │                     │                  │ ├───►─────────────────────►│
  │                     │                  │                          │ BrowserRenderer.handle(ev)
  │                     │ chunk(id,text)   │◄─── (each AgentEvent)     │
  │ chunk(id,text)      │◄─────────────────┤                          │
  │◄────────────────────┤                  │                          │
  │ ...                 │ ...              │ ...                      │
  │                     │                  │                          │ (returns assistant text)
  │                     │ done(id)         │                          │
  │ done(id)            │◄─────────────────┤                          │
  │◄────────────────────┤                  │                          │
```

Tools go through a secondary loop:

```
AgentEvent(TOOL_CALL)
    │
    │ if browser_*
    ▼
BrowserRenderer.handle → _post({tool_start, ...})
    │
    │ (tool executed via ObscuraSession's tool registry)
    │
    │ browser_read_page handler calls _call("read_page", args)
    ▼
browser_tools.py awaits future, _post({browser-tool, id, op, args})
    │
    │ SW forwards browser-tool to panel
    ▼
panel runs chrome.scripting.executeScript
    │
    │ posts browser-tool-response(id, result)
    ▼
host resolves future → handler returns → ObscuraSession continues
```

---

## Extension points (add new features here)

### Adding a new slash command
Add it to `obscura/cli/commands.py:COMMANDS`. It'll appear in the panel
autocomplete on the next host start (the host reads that dict on
startup to populate `ready.commands`). No extension code touched.

### Adding a new `$skill`
Drop a file in `~/.obscura/skills/` or commit to `obscura/core/_default_skills.py`.
Panel autocomplete picks it up from `ready.skills`.

### Adding a new `@command`
Drop in `~/.obscura/commands/`. Picked up via `ready.at_commands`.

### Adding a new browser DOM tool
Add a `ToolSpec` to `native-host/browser_tools.py:TOOLS` and an
`op` handler in `sidepanel.js:runBrowserOp`. The ToolSpec names the
op; the handler executes it via `chrome.scripting.executeScript` and
returns the result.

### Adding a new wire frame type
1. Add a handler in `_main()` of `obscura_native_host.py` (or emit
   from `BrowserRenderer.handle` / a monkey-patch).
2. Add a case in `sidepanel.js`'s `port.onMessage` switch.
3. Update the wire-protocol table in this doc + the docstring at the
   top of `obscura_native_host.py`.

### Adding a new confirmation widget kind
Patch the relevant function in `obscura/cli/widgets.py` through
`_install_widget_broker()`. The generic `widget` / `widget-response`
flow already handles arbitrary action sets.

---

## Storage

| Location | What | Cleared by |
|----------|------|------------|
| `chrome.storage.local` | Per-profile panel state: settings, transcript, session list, tab state | Extension uninstall or panel's `/clear` |
| `~/.obscura/events.db` | SQLite event log (turns, tool calls) — shared with terminal REPL | `rm` or `/session clear` |
| `~/.obscura/vector_memory/` or Qdrant | Vector memory — shared | `/memory clear` |
| `~/.obscura/logs/browser-extension-host.log` | Host stderr / logging | Manual rotation |
| `~/.obscura/browser-host.pid` | PID file (advisory lock) | Host exit |
| `~/.obscura/browser-host.env` | User secrets (GH_TOKEN, API keys) | Manual edit |

**Multi-profile warning:** two Chrome profiles running obscura on the
same machine today share `events.db` and can corrupt session ids. See
roadmap item 4.1 — use `chrome.runtime.id` to scope the path.

---

## Socket bridge (multi-process fan-out)

The native host is normally one-to-one with the panel that spawned it. To let
*separate* obscura processes (terminal REPL, REST API, headless agents) drive
the same browser, the host also opens a Unix socket at
`/tmp/obscura-browser/<user>/<pid>.sock` and runs a tiny length-prefixed JSON
RPC server on it. Multiple clients can connect concurrently; each tool call
dispatches into the same `browser_tools._call()` path that the in-host
session uses.

Discovery is via `~/.obscura/browser/active.json`, which the host maintains
on start/exit (with stale-pid pruning on every read). External processes use
the Python client at `obscura.integrations.browser.client:BrowserBridgeClient`
to find a host and dispatch calls — or `register_browser_tools(registry)` to
proxy every browser tool into an obscura `ToolRegistry`.

```
   terminal `obscura`        REST API process       headless agent
        │                          │                       │
        └──────────┬───────────────┴───────────┬───────────┘
                   │                           │
            length-prefixed JSON over Unix socket
                   │                           │
                   ▼                           ▼
        ┌────────────────────────────────────────────┐
        │  Native host  (one per Chrome profile)     │
        │    obscura_native_host.py                  │
        │    + obscura.integrations.browser.server   │  (SocketBridge)
        └─────────────────────┬──────────────────────┘
                              │ chrome native messaging
                              ▼
                    Chrome extension / panel
```

Set `OBSCURA_BROWSER_SOCKET_DISABLE=1` to keep the host but skip the socket
(useful in CI). Override the socket dir with `OBSCURA_BROWSER_SOCKET_DIR=…`.

## Choosing the right browser tool

There are two parallel families of input tools, with a sharp UX cost
difference. **Always start with the cheap family. Escalate to CDP only
when the cheap path silently does nothing, or when the feature genuinely
requires it.**

### Family 1 — event dispatch (free, silent)

`browser_fill`, `browser_click`, `browser_press_key`, `browser_eval_js`,
`browser_clipboard_read/write`. Implemented via `chrome.scripting.executeScript`
+ `dispatchEvent`. No banner, no permission prompt at runtime. **Limitation:**
synthesised events have `isTrusted=false`. Browser-default behaviours that
hang off real input (Tab moving focus, characters appearing in inputs from
keypresses, drag-drop, file picker) **will not fire**.

Covers ~80% of real-world automation: form filling, button clicks,
clipboard exchange, app-level keyboard shortcuts (`Cmd+K` palettes,
`/` search focus, `Enter` to submit, `Escape` to close).

### Family 2 — CDP (`chrome.debugger`, yellow banner)

`browser_type_text`, `browser_native_press_key`, `browser_native_click`,
`browser_upload_file`, `browser_console_logs`, `browser_network_log`,
`browser_cdp_detach`. Same wire protocol Puppeteer/Playwright use. **Cost:**
on first call per tab, Chrome attaches a debugger and shows a persistent
yellow banner *"Obscura started debugging this browser"*. Banner stays
until `browser_cdp_detach` is called or the tab closes.

Use when:
- The cheap path silently does nothing — `browser_fill` set the value but
  the page reverted it, or `browser_click` fired but nothing happened.
- The feature has no cheap-family equivalent: file uploads
  (`browser_upload_file`), console output, network logs, real Tab focus
  motion, real hover for hover-only menus.

### Recommended workflow

1. Try cheap family first.
2. Verify with `browser_read_page` or `browser_query_selector` that the
   action actually took effect.
3. If it didn't, switch to the matching CDP tool (`browser_native_click` /
   `browser_type_text` / `browser_native_press_key`).
4. When CDP work is done, call `browser_cdp_detach` to dismiss the banner.

Typical "fill + submit a form" sequence (cheap path):

```
browser_fill(selector="#email", value="me@x.com")
browser_fill(selector="#password", value="…")
browser_press_key(key="Enter", selector="#password")  # form submit
```

If the site gates submit on `isTrusted`, escalate just the keypress:

```
browser_fill(...)
browser_native_press_key(key="Enter", selector="#password")
browser_cdp_detach()
```

## Files index

```
packages/browser-extension/
├── manifest.json                     MV3 — pinned `key` = stable extension id
├── background.js                     Service-worker bridge
├── src/
│   └── sidepanel/
│       ├── index.html                Panel layout (statusbar, composer, tab strip)
│       ├── sidepanel.css             Dark-terminal theme
│       └── sidepanel.js              Panel logic (1700+ LOC, no framework)
├── native-host/
│   ├── obscura_native_host.py        Adapter — monkey-patches + main loop + socket bridge
│   ├── browser_tools.py              Browser ToolSpecs (read_page, click, fill, eval, …)
│   ├── com.obscura.host.json.tmpl    Native-messaging manifest template
│   ├── install.sh                    Generates launcher + installs manifest
│   └── obscura-native-host           (generated — not committed) launcher shell script

obscura/integrations/browser/        # Multi-process bridge — importable by any obscura process
├── wire.py                          Shared length-prefixed JSON framing
├── server.py                        SocketBridge — async Unix-socket server (used by native host)
├── client.py                        BrowserBridgeClient + register_browser_tools helper
└── active_hosts.py                  ~/.obscura/browser/active.json registry
├── .keys/
│   ├── EXTENSION_ID                  Pinned id derived from public key
│   ├── extension.pub.b64             Public key for manifest.json "key" field
│   │                                 (private key lives in ../../browser-extension-keys/extension.pem)
│   └── README.md                     Key management notes
├── icons/                            16/32/48/128 PNGs
└── tests/                            see ../../tests/browser_extension/
```

---

## Debugging in 30 seconds

1. **Panel is "disconnected"** → `tail -f ~/.obscura/logs/browser-extension-host.log`.
   If no recent entries, the host isn't launching. Check
   `chrome://extensions` → *service worker* for the SW console.
2. **Host launched but no `ready` frame in panel** → SW bridge dropped
   the frame. SW console will show a stack trace.
3. **Commands/skills missing from autocomplete** → host imported but
   `_available_commands()` hit an ImportError. Check the log.
4. **Tool calls return nothing** → panel couldn't execute the op.
   Panel DevTools console will have the thrown error.
5. **Changes to launcher or env aren't taking effect** → old host is
   still running. `obscura-browser reload` or `pkill -f
   obscura_native_host.py`, then reload the Obscura card on
   `chrome://extensions`.

---

## Non-goals

- **Not a web app.** The SW and host assume a single privileged user on
  the local machine. Don't add server-side rendering or remote hosts.
- **Not a UI framework playground.** Keep `sidepanel.js` dependency-free
  — if you reach for React, commit to it everywhere and document the
  migration in this file.
- **Not a separate obscura surface.** If a feature makes sense in the
  terminal REPL, land it in `obscura.cli.commands` first; the panel
  inherits it automatically.

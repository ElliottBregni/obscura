# WhatsApp via wuzapi (free, personal account)

A free WhatsApp transport for Obscura that uses your **personal WhatsApp
account** as a linked device, without paying for Twilio or signing up for
Meta's Cloud API. Inbound messages land directly in the Obscura REPL;
agent replies go back via WhatsApp.

## How it works

```
Your phone (WhatsApp)
        │  multi-device WhatsApp Web protocol
        ▼
  wuzapi (Go sidecar, LaunchAgent)         ← holds the linked-device session
        │  POST :18794/inbound  (HMAC-signed)
        ▼
  obscura whatsapp daemon                  ← Starlette receiver
        │  channel_inject._queue (UDS fan-out)
        ▼
  obscura REPL                              ← message appears as input
        │
        ▼ agent.run() → reply
  WuzapiAdapter.send → wuzapi → WhatsApp servers → your contact's phone
```

**wuzapi** is a Go REST wrapper around
[whatsmeow](https://github.com/tulir/whatsmeow), the reverse-engineered
WhatsApp Web protocol library. We run it as a LaunchAgent-managed sidecar
on `127.0.0.1:18793`. Obscura's Python adapter (`WuzapiAdapter`) is a
thin HTTP client + event-to-`PlatformMessage` translator on top of it.

## Setup

Prerequisites: `git`, `go` (1.25+), Python 3.13.

```bash
# 1. Clone, build, install LaunchAgent for wuzapi
obscura whatsapp install

# 2. Create the wuzapi user, scan QR with your phone
obscura whatsapp link
# (opens QR in Preview on macOS — scan via WhatsApp → Settings → Linked Devices)

# 3. Verify the link
obscura whatsapp status
# WhatsApp session  loggedIn: True  jid: 12316333624:14@s.whatsapp.net

# 4. Opt into the wuzapi transport in your obscura config
cat >> ~/.obscura/config.toml <<EOF

[messaging.whatsapp]
enabled = true
transport = "wuzapi"
mode = "channel_inject"
EOF

# 5. Start the inbound bridge in a separate terminal
obscura whatsapp daemon
# wuzapi daemon: listening on 127.0.0.1:18794/inbound
# Ctrl-C to stop

# 6. In yet another terminal, run the REPL
obscura
```

Now any WhatsApp message you receive will appear in the REPL as input
prefixed with `[whatsapp from +1...]:` and Molty replies will go back
through your WhatsApp.

## Commands

| Command | Effect |
|---|---|
| `obscura whatsapp install` | Idempotent — clone, build, write plist, generate secrets, kickstart sidecar |
| `obscura whatsapp link` | Create wuzapi user (if missing), show QR, poll until linked |
| `obscura whatsapp status` | Service state + WhatsApp session state |
| `obscura whatsapp logs -n 50` | Tail wuzapi.log + wuzapi.err.log |
| `obscura whatsapp send +12345 "hi"` | Outbound test (uses linked device) |
| `obscura whatsapp daemon` | Run inbound bridge (REPL needs this for inbound) |
| `obscura whatsapp uninstall` | Stop sidecar, remove LaunchAgent. `--wipe-state` also deletes the session DB |

## Files & ports

| Path / Port | Owner | Purpose |
|---|---|---|
| `~/.obscura/wuzapi/src/` | upstream | wuzapi source (gitignored from obscura's tree) |
| `~/.obscura/wuzapi/wuzapi` | install | Compiled Go binary |
| `~/.obscura/wuzapi/state/dbdata/` | wuzapi | WhatsApp session DB (SQLite) |
| `~/.obscura/wuzapi/{admin,user}.token` | install | Bearer tokens (mode 600) |
| `~/.obscura/wuzapi/{hmac,encryption}.key` | install | Wuzapi internal secrets |
| `~/.obscura/wuzapi/.env` | install | Loaded by wuzapi at startup |
| `~/Library/LaunchAgents/dev.obscura.wuzapi.plist` | install | macOS LaunchAgent |
| `~/.obscura/logs/wuzapi.{log,err.log}` | wuzapi | stdout / stderr |
| `127.0.0.1:18793` | wuzapi | REST API (loopback only) |
| `127.0.0.1:18794` | daemon | Inbound webhook receiver (loopback only) |

## Design notes

* **Why a sidecar, not in-process**: wuzapi/whatsmeow is Go, Obscura is
  Python. A REST sidecar (rather than a Python subprocess of Baileys)
  keeps the WhatsApp session alive across Obscura REPL restarts and gives
  us a clean process boundary for debugging.
* **Why loopback-only**: the wuzapi admin token grants full control of
  your linked-device session. Binding to `127.0.0.1` ensures only local
  processes can reach it. Webhook receiver too.
* **Why polling isn't an option**: wuzapi has no `GET /messages?since=…`
  endpoint. Inbound events come exclusively via webhook. We accept that
  and run a small Starlette receiver — only when the feature is enabled.
* **Clean opt-in**: when `[messaging.whatsapp].enabled = false` (or the
  section is missing), nothing runs in Obscura's process. The wuzapi
  LaunchAgent still runs (it's separate) but Obscura doesn't bridge to
  it. The off state is truly off.
* **Typing indicator while the agent composes**: when an inbound message
  arrives, the wuzapi service sends `composing` presence to the chat
  before pushing the message into the REPL queue. A background keepalive
  re-sends `composing` every 8s (WhatsApp clears the bubble after ~10s
  of presence-channel silence). When the agent's `reply_fn` fires, the
  service sends `paused` to clear the indicator — wrapped in try/finally
  so the bubble always clears, even on rate-limit drops or send errors.
  A hard 60s cap clears any dangling indicator if the agent decides not
  to reply at all. All presence errors are swallowed: a failing typing
  call never blocks the real reply.
* **Multi-REPL: first-wins + auto-promotion**: only one process can bind
  `127.0.0.1:18794`. The first REPL to start wins and becomes the
  **OWNER** — it hosts the webhook receiver, parses inbound, and fans
  out via UDS to every other REPL. Subsequent REPLs become **PEERs**:
  they display inbound messages locally (via the UDS fanout) and spawn
  a background watcher that re-probes the port every ~5s. When the
  owner REPL exits, the next peer's probe wins the port and prints
  `[wuzapi] AUTO-PROMOTED` — the bridge keeps running, no manual
  restart. Look for `OWNER` / `PEER` / `AUTO-PROMOTED` in your REPL
  boot output to know which role you're in. Replies only ever send
  from the owner: peer-injected messages get a no-op reply_fn
  ([uds_inbox.py](../../../composition/blocks/uds_inbox.py)) so you
  don't get N replies per inbound across N open REPLs.

## Security

* Both bearer tokens (`admin.token`, `user.token`) live in mode-600 files.
  Anyone with these tokens can read/send your WhatsApp.
* The webhook bridge is HMAC-protected by `wuzapi`'s global key
  (`hmac.key`). Forged POSTs to the receiver are rejected — though
  loopback binding is the primary defense.
* Disable the integration cleanly:
  `obscura whatsapp uninstall` (preserves session) or
  `obscura whatsapp uninstall --wipe-state` (also removes the linked
  device — you'd need to re-scan QR to relink).

## Bug postmortem

Every distinct bug we hit while building this out, with the symptom you'd
see, the root cause, and where the fix lives. Future maintainers (or
future-me) should consult this before re-debugging.

| # | Symptom | Root cause | Fix location |
|---|---|---|---|
| 1 | Webhook receives POSTs but `json.loads` fails with `Expecting value` | wuzapi sends `application/x-www-form-urlencoded` with the event payload nested under a `jsonData` field — not raw JSON | [`webhook.py`](webhook.py) — content-type sniff + form-field extraction |
| 2 | REPL loops on a runaway "rock museum" / "non rddd" style joke escalation after one inbound message; per-message queue overflow warnings | `discover_peers()` listed our own `.sock` file in `~/.obscura/sockets/` → `push_channel_message` UDS-fanned-out to *us* → `UDSInbox` re-injected into the local queue → next agent turn re-fanned-out → … | [`uds_messaging.py`](../../../kairos/uds_messaging.py) — `_LOCAL_SESSION_IDS` set + `discover_peers()` self-filter |
| 3 | Replies typed from your phone never reached the REPL | wuzapi marks self-originated messages with `IsFromMe=true`; the adapter dropped these as echoes, even when they were genuine inbound (you texting yourself, group activity routed via your account) | [`adapter.py`](adapter.py) — IsFromMe is now one input to a layered echo check, not an absolute filter |
| 4 | Echo detection let some self-sent replies through; loops re-amplified | We were matching only by message ID. wuzapi assigns different ID prefixes (`3EB0…` vs `3A…`) depending on direction and re-encryption — so the IDs we *sent* didn't match the IDs that came *back* | [`adapter.py`](adapter.py) — added text-content match in a 60s window alongside ID match |
| 5 | Replies to group chats or self-chats landed in random DMs (or got rejected as invalid phone) | We were stripping the JID suffix (`@s.whatsapp.net`, `@g.us`, `@lid`) to a bare digit string before re-using it as a reply target. Bare LID numerics look like phone numbers to wuzapi but aren't routable | [`service.py`](service.py) — `_to_channel_message` uses full `metadata['jid_chat']` JID as reply target; [`adapter.py`](adapter.py) — `send()` passes `@`-containing recipients through verbatim |
| 6 | Each inbound message duplicated N times in the REPL, N = number of open obscura terminals | Stale `.sock` files from prior REPL crashes or unclean exits still satisfied the `sock_file.exists()` peer check. UDS fanout shipped to every "live" peer including ghosts | [`wuzapi_daemon.py`](../../../composition/blocks/wuzapi_daemon.py) — `_cleanup_dead_session_state()` removes lockfiles whose PIDs are dead + orphan sockets at REPL boot |
| 7 | Composition block prints nothing during REPL boot; no `[wuzapi]` banner; bridge silently never started | Block was copy-pasted from `imessage_daemon` which gates on `session.supervisor is None`. The wuzapi bridge is an HTTP receiver, not a supervised agent — the gate doesn't apply | [`wuzapi_daemon.py`](../../../composition/blocks/wuzapi_daemon.py) — supervisor gate removed; visible `print(..., flush=True)` for every gate hit |
| 8 | "channel queue full, dropped message" warnings spam stdout under sustained inbound | Cascading effect of bug #2 — once a self-broadcast loop starts, every iteration adds one more entry to the bounded `_queue` until the bound is hit | Same as #2 — the queue is fine, the producer was misbehaving |
| 9 | Sending a multi-line burst from your phone (typing one fragment at a time) triggered an agent turn per fragment, each producing its own reply → frantic conversation | No coalescing layer between webhook → REPL queue. Each `Message` event fired a separate `push_channel_message` | [`service.py`](service.py) — `_DebouncedDispatcher` with a 2.5s per-sender window, last-message wins for metadata, newline-joined text |
| 10 | Agent received "" / "\n" / single-char garbage events and burned LLM tokens replying to them | wuzapi emits `Message` events for non-text payloads (media, reactions, deletions) where `text` is empty or whitespace-only | [`service.py`](service.py) — strip-then-check; [`adapter.py`](adapter.py) — also drops blank-text upstream so debounce never sees them |

### Defense-in-depth additions (not bug fixes per se)

| Layer | What it does | Why we kept it |
|---|---|---|
| Per-sender reply rate limit | 3s min-gap between consecutive replies + 20/hr hard cap (see `_ReplyRateLimit` in [`service.py`](service.py)) | Even with #2 fixed, *any* future feedback loop bug (mis-tuned auto-responder, accidental thread join) caps at 20 replies/hour per sender — well below "user notices something is wrong" |
| Port-bind probe before uvicorn | Try `socket.bind` first; bail if `EADDRINUSE` | Stops crash-loop when the standalone `whatsapp-daemon` LaunchAgent already owns `:18794`. We just rely on its UDS broadcasts in that case |
| Webhook auto-configure each boot | `set_webhook` is idempotent and cheap | Defends against config drift if a third party stomps wuzapi's stored webhook URL |

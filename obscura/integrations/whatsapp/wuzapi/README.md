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

# Two-Node Agent Network Topology

Full reference for the Obscura + OpenClaw two-node agent mesh — local loopback
and Tailscale layers, ports, auth, message flows, shared state, and operational
procedures.

---

## 1. ASCII Diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                          Elliott's MacBook Pro                               ║
║                                                                              ║
║  ┌──────────────────────────────┐       ┌──────────────────────────────────┐ ║
║  │  Node 1 — OpenClaw (Molty)  │       │  Node 2 — Obscura (Claude)       │ ║
║  │                              │       │                                  │ ║
║  │  Gateway    localhost:18789  │       │  Network GW   0.0.0.0:18790      │ ║
║  │  (WS + HTTP, loopback only) │       │  (OpenAI-compat, A2A, WS)        │ ║
║  │                              │       │                                  │ ║
║  │  Model: Kimi K2.5 (primary) │       │  A2A server   localhost:8080      │ ║
║  │  Also: moonshot, openrouter │       │  (original SDK server)           │ ║
║  │         github               │       │                                  │ ║
║  │                              │       │  Backends: claude, copilot,      │ ║
║  │  Peer config:                │       │            codex, localllm       │ ║
║  │    a2aPeers → 18790          │       │                                  │ ║
║  │  Model provider:             │       │  OpenClawBridge → 18789          │ ║
║  │    obscura/* → 18790/v1      │       │  ask_openclaw tool (all backends)│ ║
║  └──────────────────────────────┘       └──────────────────────────────────┘ ║
║           │  ▲                                    │  ▲                        ║
║           │  │  (A)                         (B)  │  │                        ║
║           │  └────────────────────────────────────┘  │                        ║
║           │                                           │                        ║
║           │  (A) OpenClaw→Obscura                     │                        ║
║           │      A2A REST/SSE/JSON-RPC on :18790      │                        ║
║           │      OR chat completions on :18790/v1     │                        ║
║           │                                           │                        ║
║           │  (B) Obscura→OpenClaw                     │                        ║
║           └───────────────────────────────────────────┘                        ║
║               POST /v1/chat/completions on :18789                              ║
║                                                                                ║
║  ┌─────────────────────────────────────────────────────────────────────────┐  ║
║  │  Shared state layer (file-based)                                        │  ║
║  │  ~/.obscura/vault/shared/   — async message handoff                     │  ║
║  │  ~/.obscura/sessions/*.lock — agent monitor polls every 15s (PID 47368) │  ║
║  └─────────────────────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════════════════════╝

                          TAILSCALE OVERLAY
╔══════════════════════════════════════════════════════════════════════════════╗
║  tailnet: tail91e620.ts.net                                                  ║
║                                                                              ║
║  OpenClaw (Molty)                      Obscura (Claude)                      ║
║  wss://modernizedai.tail91e620.ts.net  https://elliotts-macbook-pro-1        ║
║         (remote WS endpoint)                  .tail91e620.ts.net             ║
║                                               (proxies → localhost:18790)    ║
║                                                                              ║
║  Remote clients can reach either node via Tailscale without opening any     ║
║  public firewall ports.  Both nodes must be connected to the tailnet.       ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 2. Port Map

| Port  | Bound on       | Node         | Protocol(s)                        | Purpose                                              |
|-------|----------------|--------------|------------------------------------|----------------------------------------------------- |
| 18789 | 127.0.0.1      | OpenClaw     | HTTP + WebSocket                   | OpenClaw gateway (loopback only)                    |
| 18790 | 0.0.0.0        | Obscura      | HTTP (OpenAI-compat, A2A, WS)      | Obscura network gateway — primary inter-node port    |
| 18792 | 0.0.0.0        | Obscura      | HTTP + WebSocket + HTML chat UI    | Standalone agent — direct browser/Tailscale chat     |
| 443   | Tailscale VIP  | OpenClaw     | WSS (HTTPS tunnel)                 | `modernizedai.tail91e620.ts.net` remote WS          |
| 443   | Tailscale VIP  | Obscura GW   | HTTPS (tunnel → 18790)             | `elliotts-macbook-pro-1.tail91e620.ts.net`          |
| 18792 | Tailscale VIP  | Obscura SA   | HTTPS (tunnel → 18792)             | `elliotts-macbook-pro-1.tail91e620.ts.net:18792`    |

**Route map within port 18790 (main gateway):**

| Path                              | Method    | Auth          | Description                            |
|-----------------------------------|-----------|---------------|----------------------------------------|
| `/v1/chat/completions`            | POST      | Bearer        | OpenAI-compatible completions          |
| `/v1/models`                      | GET       | Bearer        | List Obscura backends as model objects |
| `/v1/chat/ws`                     | WebSocket | Bearer/api_key| Bidirectional streaming chat + presence|
| `/a2a/rpc`                        | POST      | Bearer        | A2A JSON-RPC                           |
| `/a2a/v1/tasks`                   | POST/GET  | Bearer        | A2A REST task management               |
| `/a2a/v1/tasks/streaming`         | GET (SSE) | Bearer        | A2A SSE streaming responses            |
| `/.well-known/agent.json`         | GET       | Public        | A2A agent card                         |
| `/health`                         | GET       | Public        | Liveness probe                         |
| `/channels/telegram/webhook`      | POST      | HMAC token    | Telegram Bot API inbound updates       |
| `/channels/whatsapp/verify`       | GET       | Hub challenge | WhatsApp webhook verification          |
| `/channels/whatsapp/webhook`      | POST      | HMAC sig      | WhatsApp Cloud API inbound messages    |
| `/channels/configs`               | POST/GET  | Bearer        | Channel config CRUD                    |
| `/channels/configs/{id}/apply`    | POST      | Bearer        | Hot-reload channel config to live router|
| `/webhook/a2a`                    | POST      | HMAC-SHA256   | A2A push notification callbacks        |
| `/peers/openclaw/.well-known/agent.json` | GET | Public   | Synthetic A2A card for OpenClaw bridge |

**Route map within port 18792 (standalone agent):**

| Path                              | Method    | Auth          | Description                            |
|-----------------------------------|-----------|---------------|----------------------------------------|
| `/`                               | GET       | Public        | Embedded dark-theme browser chat UI    |
| `/ws`                             | WebSocket | Bearer/api_key| Streaming chat + platform messages + presence |
| `/v1/chat/completions`            | POST      | Bearer        | OpenAI-compatible completions          |
| `/v1/models`                      | GET       | Bearer        | List Obscura backends                  |
| `/channels/telegram/webhook`      | POST      | HMAC token    | Telegram inbound (mirrored from :18790)|
| `/channels/whatsapp/webhook`      | POST      | HMAC sig      | WhatsApp inbound (mirrored from :18790)|
| `/health`                         | GET       | Public        | Liveness probe                         |

---

## 3. Auth Map

| Token / Secret               | Held by          | Used for                                               | Resolution order                                                                             |
|------------------------------|------------------|--------------------------------------------------------|---------------------------------------------------------------------------------------------|
| `4a30d783...eda8754a`        | Obscura          | Outbound calls from `OpenClawBridge` → OpenClaw :18789 | Hardcoded in `OpenClawBridgeConfig.token`; falls back to `OPENCLAW_TOKEN` env var           |
| `~/.obscura/network-gateway.token` | Obscura    | Inbound bearer auth on Obscura network gateway :18790  | `OBSCURA_NETWORK_TOKEN` env var → `~/.obscura/network-gateway.token` → empty (no auth)     |
| `~/.obscura/network-gateway-webhook.secret` | Obscura | HMAC-SHA256 signing of push notification callbacks | `OBSCURA_WEBHOOK_SECRET` env var → `~/.obscura/network-gateway-webhook.secret`    |
| `~/.openclaw/openclaw.json` → `a2aPeers[].token` | OpenClaw | OpenClaw→Obscura outbound calls :18790 | Read by OpenClaw at startup; set to match `~/.obscura/network-gateway.token`               |
| `TELEGRAM_WEBHOOK_SECRET`    | Obscura          | Verify `X-Telegram-Bot-Api-Secret-Token` header       | `~/.obscura/config.toml` `[messaging.telegram] webhook_secret` → env var                   |
| `WHATSAPP_APP_SECRET`        | Obscura          | Verify `X-Hub-Signature-256` on WhatsApp webhooks     | `~/.obscura/config.toml` `[messaging.whatsapp] app_secret` → env var                      |
| `WHATSAPP_VERIFY_TOKEN`      | Obscura          | Verify Meta hub challenge on `/channels/whatsapp/verify` | `~/.obscura/config.toml` `[messaging.whatsapp] verify_token` → env var                  |

**Middleware stack on port 18790 (outermost → innermost):**

```
RequestSizeLimit    (1 MB max body; exempt: /health, /.well-known/, /peers/)
  → SecurityHeaders
  → GatewayRateLimit    (60 req/min/IP; exempt: /health, /.well-known/, /peers/)
  → WebhookRateLimit    (20 req/min/IP on /webhook/ and /channels/*/webhook paths)
  → GatewayBearerAuth   (exempt: /health, /.well-known/, /peers/, /webhook/,
                                  /channels/telegram/webhook, /channels/whatsapp/)
  → CORS
  → routes
```

`strict_webhook_verification = True` (default): webhook endpoints return `503` if
their respective HMAC secret is not configured — preventing silent unauthenticated
delivery.

Exempt from all auth+rate middleware: `/health`, `/.well-known/`, `/peers/`.

---

## 4. Message Flows

### 4.1 Obscura → OpenClaw (single-turn)

```
Caller / Tool          OpenClawBridge              OpenClaw Gateway :18789
     │                       │                              │
     │  bridge.send(text)    │                              │
     │──────────────────────►│                              │
     │                       │  POST /v1/chat/completions   │
     │                       │  Authorization: Bearer <tok> │
     │                       │  {"model":"openclaw/main",   │
     │                       │   "messages":[...],          │
     │                       │   "stream":false}            │
     │                       │─────────────────────────────►│
     │                       │                              │ Kimi K2.5 inference
     │                       │  200 {"choices":[{           │
     │                       │    "message":{"content":"…"}}│
     │                       │  ]}                          │
     │                       │◄─────────────────────────────│
     │  A2ATask (completed)  │                              │
     │◄──────────────────────│                              │
```

**Endpoint:** `POST http://localhost:18789/v1/chat/completions`
**Auth:** `Authorization: Bearer 4a30d783737e2aac23148de52a29d9b820cffba3eda8754a`
**Retry:** up to `ObscuraConfig.max_retries` on 5xx/transport errors; circuit breaker trips after N failures.
**Audit log:** `~/.obscura/logs/a2a-bridge.jsonl`

### 4.2 Obscura → OpenClaw (streaming)

```
Caller                 OpenClawBridge              OpenClaw Gateway :18789
     │                       │                              │
     │  bridge.stream_send() │                              │
     │──────────────────────►│                              │
     │                       │  POST /v1/chat/completions   │
     │                       │  {"stream":true}             │
     │                       │─────────────────────────────►│
     │                       │  SSE: data: {"choices":[{    │
     │   yield A2AStatus-    │    "delta":{"content":"…"}}]}│
     │   UpdateEvent(working)│◄─────────────────────────────│
     │◄──────────────────────│  (token by token)            │
     │         ...           │                              │
     │                       │  data: [DONE]                │
     │                       │◄─────────────────────────────│
     │  yield final event    │                              │
     │  (completed)          │                              │
     │◄──────────────────────│                              │
```

Fallback: if OpenClaw does not support streaming, `stream_send` falls back to `send()` and emits a single completed event.

### 4.3 OpenClaw → Obscura via A2A REST (JSON-RPC)

```
OpenClaw                        Obscura Network GW :18790
     │                                    │
     │  POST /a2a/rpc                     │
     │  Authorization: Bearer <obscura-tok>
     │  {"jsonrpc":"2.0",                 │
     │   "method":"message/send",         │
     │   "params":{"message":{...}},      │
     │   "id":"<req-id>"}                 │
     │───────────────────────────────────►│
     │                                    │ agent loop execution
     │  {"jsonrpc":"2.0",                 │
     │   "result": <Task>,                │
     │   "id":"<req-id>"}                 │
     │◄───────────────────────────────────│
```

**Endpoint:** `POST http://localhost:18790/a2a/rpc`
**Auth:** `Authorization: Bearer <value from ~/.obscura/network-gateway.token>`

### 4.4 OpenClaw → Obscura via A2A REST (task REST)

```
OpenClaw                        Obscura Network GW :18790
     │                                    │
     │  POST /a2a/v1/tasks                │
     │  {"message":{...}}                 │
     │───────────────────────────────────►│
     │  201 {"id":"<task-id>","status":{}}│
     │◄───────────────────────────────────│
     │                                    │
     │  GET /a2a/v1/tasks/<task-id>       │  (poll or subscribe)
     │───────────────────────────────────►│
     │  200 <Task>                        │
     │◄───────────────────────────────────│
```

### 4.5 OpenClaw → Obscura via A2A SSE (streaming)

```
OpenClaw                        Obscura Network GW :18790
     │                                    │
     │  GET /a2a/v1/tasks/streaming       │
     │  (long-lived connection)           │
     │───────────────────────────────────►│
     │  event: status-update              │
     │  data: {"state":"working",...}     │
     │◄───────────────────────────────────│
     │         ...                        │
     │  event: status-update              │
     │  data: {"state":"completed",...,   │
     │         "final":true}              │
     │◄───────────────────────────────────│
```

### 4.6 OpenClaw → Obscura via chat completions

OpenClaw can treat Obscura backends as model providers using the OpenAI-compat API:

```
OpenClaw                        Obscura Network GW :18790
     │                                    │
     │  POST /v1/chat/completions         │
     │  {"model":"obscura/claude",        │
     │   "messages":[...]}               │
     │───────────────────────────────────►│
     │                                    │ routes to claude/copilot/codex backend
     │  {"choices":[{"message":{...}}]}  │
     │◄───────────────────────────────────│
```

**Available model strings:** `obscura/claude`, `obscura/copilot`, `obscura/codex`
**Base URL for OpenClaw config:** `http://localhost:18790/v1`

### 4.7 Push notification callback (Obscura → itself via webhook)

After a blocking A2A task completes, Obscura fires a push notification to the
URL supplied in `params.pushNotificationUrl`:

```
A2A caller (OpenClaw)         Obscura GW :18790        /webhook/a2a handler
     │                              │                         │
     │  POST /a2a/rpc               │                         │
     │  {"method":"message/send",   │                         │
     │   "params":{                 │                         │
     │     "message":{...},         │                         │
     │     "pushNotificationUrl":   │                         │
     │       "http://localhost:18790│                         │
     │        /webhook/a2a"}}       │                         │
     │─────────────────────────────►│                         │
     │                              │  agent loop (Claude)    │
     │                              │  blocking execution     │
     │  {"result": Task(completed)} │                         │
     │◄─────────────────────────────│                         │
     │                              │  POST /webhook/a2a      │
     │                              │  X-Webhook-Signature:   │
     │                              │    sha256=<hmac>        │
     │                              │─────────────────────────►
     │                              │                         │ verify HMAC
     │                              │  {"ok":true,...}        │
     │                              │◄─────────────────────────
```

The gateway verifies `X-Webhook-Signature` using the shared secret from
`~/.obscura/network-gateway-webhook.secret`.  The A2A service signs callbacks
with the same secret (`_fire_push_notification` in `a2a/service.py`).

### 4.8 Remote access via Tailscale

```
Remote client
     │
     │  WSS / HTTPS
     │
     ▼
modernizedai.tail91e620.ts.net          → OpenClaw :18789 (WebSocket)
elliotts-macbook-pro-1.tail91e620.ts.net → Obscura  :18790 (all routes)
```

Tailscale `serve` is configured at gateway startup when `GatewayConfig.tailscale_enabled = True`. The `tailscale serve --bg https+insecure://localhost:18790` command creates the proxy mapping; `remove_tailscale_serve` tears it down on shutdown.

**Note:** Tailscale access goes through `GatewayBearerAuthMiddleware` — remote
callers must supply the same bearer token as local callers.

### 4.9 Platform message fanout (WhatsApp / Telegram → all WS clients)

A single process-level `ConnectionRegistry` (in `connections.py`) owns one
`channel_inject` subscription.  Every connected WS client — on either port —
receives every platform message in real time:

```
External platform          Obscura :18790              ConnectionRegistry
(WhatsApp / Telegram)       /channels/*/webhook              (fanout task)
     │                            │                                │
     │  POST /channels/…/webhook  │                                │
     │  X-Hub-Signature-256: …    │                                │
     │───────────────────────────►│                                │
     │                            │ verify HMAC, parse update      │
     │                            │ channel_router.dispatch(…)     │
     │                            │                                │
     │                            │  push_channel_message(msg)     │
     │                            │───────────────────────────────►│
     │  200 {"status":"ok"}       │                                │ broadcast to all
     │◄───────────────────────────│                                │ WS clients:
     │                            │                                │ {"type":"incoming",
     │                            │                                │  "platform":"whatsapp",
     │                            │                                │  "sender":"…",
     │                            │                                │  "text":"…"}
     │                            │       ◄────────────────────────│
     │                            │  /v1/chat/ws client(s)         │
     │                            │  /ws client(s)                 │
     │                            │  (all receive the frame)       │
```

When a WS client responds, the registry's `_active_reply` callback routes the
reply back to the originating platform channel.

### 4.10 Presence broadcasts

Every WS connection (on `/v1/chat/ws` or `/ws`) registers with the
`ConnectionRegistry` on accept and deregisters on disconnect.  All other
connected clients receive a presence frame:

```
Client A connects → registry.register() →
    broadcast {"type":"presence","event":"connected","conn_id":"a1b2c3d4","count":2}
    → received by Client B (if any)

Client A disconnects → registry.unregister() →
    broadcast {"type":"presence","event":"disconnected","conn_id":"a1b2c3d4","count":1}
    → received by Client B (if any)
```

The standalone agent chat UI (`/`) displays the live client count in its
status bar via these frames.

### 4.11 Standalone agent direct chat

```
Browser (Tailscale or LAN)            Obscura Standalone Agent :18792
     │                                               │
     │  GET /                                        │
     │──────────────────────────────────────────────►│
     │  200 (dark-theme chat HTML + JS)              │
     │◄──────────────────────────────────────────────│
     │                                               │
     │  WS /ws                                       │
     │  (upgrade; token via Bearer or ?api_key=)     │
     │──────────────────────────────────────────────►│ register in ConnectionRegistry
     │                                               │ broadcast presence/connected
     │  {"type":"presence","event":"connected","count":1}
     │◄──────────────────────────────────────────────│
     │                                               │
     │  {"type":"message","content":"Hello!"}        │
     │──────────────────────────────────────────────►│
     │  {"type":"token","content":"Hi","session_id":"…"}  (streaming)
     │◄──────────────────────────────────────────────│
     │  {"type":"done","session_id":"…"}             │
     │◄──────────────────────────────────────────────│
```

---

## 5. Shared State

### Vault handoff (`~/.obscura/vault/shared/`)

File-based async message passing between nodes. Either node writes a JSON file; the other polls and consumes it. Used for: deferred task results, large artifact payloads, cross-session context snapshots.

**File naming convention:** `<sender>-<timestamp>-<uuid>.json`

**Schema (informal):**
```json
{
  "from": "openclaw | obscura",
  "to":   "openclaw | obscura",
  "type": "message | artifact | context",
  "payload": { ... },
  "created_at": "<ISO-8601>"
}
```

Files are consumed (deleted) after successful read. Stale files (> 1h) are pruned by the agent monitor.

### Session locks (`~/.obscura/sessions/*.lock`)

Each active Obscura session writes a PID lock file. The agent monitor (PID 47368) polls this directory every 15 seconds to detect crashed sessions and trigger cleanup or recovery.

### Audit logs

| Path                                       | Written by       | Content                              |
|--------------------------------------------|------------------|--------------------------------------|
| `~/.obscura/logs/a2a-bridge.jsonl`         | OpenClawBridge   | Per-call timing, token count, state  |
| `~/.obscura/logs/deep.jsonl`               | ToolRegistry     | Per-tool-call audit trail            |
| `~/.obscura/events.db`                     | SQLiteEventStore | Full agent event log                 |

---

## 6. Adding a Third Node

Adding a third node (e.g. a new agent runtime on another machine or port) requires three steps:

### Step 1 — Register the peer in Obscura's well-known registry

```python
# In obscura/integrations/a2a/well_known.py — add near the openclaw entry:
from obscura.integrations.a2a.well_known import DEFAULT_REGISTRY, WellKnownAgent

DEFAULT_REGISTRY.register(WellKnownAgent(
    name="node3",
    url="http://node3-host:PORT",   # or https:// for Tailscale
    auth_token="<node3-bearer-token>",
))
```

Or at runtime via config TOML:
```toml
# ~/.obscura/config.toml
[[network.well_known_agents]]
name = "node3"
url  = "http://node3-host:PORT"
auth_token = "env:NODE3_TOKEN"
```

### Step 2 — Create a bridge (if the peer does not speak A2A natively)

```python
from obscura.integrations.a2a.openclaw_bridge import OpenClawBridge, OpenClawBridgeConfig

node3_bridge = OpenClawBridge(OpenClawBridgeConfig(
    token="<node3-bearer-token>",
    gateway_url="http://node3-host:PORT",
    model="node3/main",
))
```

If the peer speaks A2A natively, use `A2AClient` directly (no bridge needed).

### Step 3 — Configure the third node to reach Obscura

On the new node:
- Set `a2aPeers` URL to `http://<obscura-host>:18790` (or the Tailscale URL).
- Set the bearer token to the value in `~/.obscura/network-gateway.token`.
- For model-provider access, set base URL to `http://<obscura-host>:18790/v1` and use model strings `obscura/claude`, `obscura/copilot`, `obscura/codex`.

For Tailscale access from a remote node:
- Point the peer at `https://elliotts-macbook-pro-1.tail91e620.ts.net` — no port needed (Tailscale terminates TLS on 443 and proxies to 18790).
- The node must be on the `tail91e620` tailnet.

---

## 7. Troubleshooting — Verifying Each Link

### 7.1 Is Obscura's network gateway up?

```bash
curl -s http://localhost:18790/health
# Expected: {"status":"ok","service":"obscura-network-gateway","port":18790}
```

### 7.2 Is the OpenClaw gateway up?

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer 4a30d783737e2aac23148de52a29d9b820cffba3eda8754a" \
  http://localhost:18789/v1/models
# Expected: 200
```

### 7.3 Can Obscura reach OpenClaw? (bridge smoke test)

```bash
curl -s \
  -H "Authorization: Bearer 4a30d783737e2aac23148de52a29d9b820cffba3eda8754a" \
  -H "Content-Type: application/json" \
  -d '{"model":"openclaw/main","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
  http://localhost:18789/v1/chat/completions
# Expected: {"choices":[{"message":{"content":"..."}}], ...}
```

### 7.4 Can OpenClaw reach Obscura? (A2A REST)

```bash
TOKEN=$(cat ~/.obscura/network-gateway.token)
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","params":{"message":{"role":"user","parts":[{"type":"text","text":"ping"}]}},"id":"t1"}' \
  http://localhost:18790/a2a/rpc
# Expected: {"jsonrpc":"2.0","result":{...},"id":"t1"}
```

### 7.5 Obscura agent card / discovery

```bash
curl -s http://localhost:18790/.well-known/agent.json | python3 -m json.tool
# Expected: JSON agent card with name, capabilities, skills
```

### 7.6 Obscura OpenAI-compat endpoint

```bash
TOKEN=$(cat ~/.obscura/network-gateway.token)
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"obscura/claude","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
  http://localhost:18790/v1/chat/completions
# Expected: {"choices":[{"message":{"content":"..."}}], ...}
```

### 7.7 Tailscale connectivity — Obscura

```bash
curl -s https://elliotts-macbook-pro-1.tail91e620.ts.net/health
# Expected: {"status":"ok",...}
# If 000 / connection refused: check `tailscale status` and `tailscale serve status`
```

### 7.8 Tailscale connectivity — OpenClaw

```bash
# WebSocket ping via wscat (npm i -g wscat)
wscat -c "wss://modernizedai.tail91e620.ts.net/v1/chat/ws" \
  -H "Authorization: Bearer 4a30d783737e2aac23148de52a29d9b820cffba3eda8754a"
# Expected: WebSocket handshake succeeds
```

### 7.9 Tailscale serve status

```bash
tailscale serve status
# Should show: localhost:18790 → https://elliotts-macbook-pro-1.tail91e620.ts.net
```

### 7.10 Webhook HMAC smoke test

```bash
SECRET=$(cat ~/.obscura/network-gateway-webhook.secret)
PAYLOAD='{"type":"test","from":"smoke-test"}'
SIG="sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -s \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: $SIG" \
  -d "$PAYLOAD" \
  http://localhost:18790/webhook/a2a
# Expected: {"ok":true,...}
# If 401: secret mismatch or gateway not reloaded after secret was created
```

### 7.11 Full A2A round-trip with push notification

```bash
TOKEN=$(cat ~/.obscura/network-gateway.token)
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0",
    "method":"message/send",
    "params":{
      "message":{"role":"user","parts":[{"type":"text","text":"reply with: pong"}]},
      "pushNotificationUrl":"http://localhost:18790/webhook/a2a"
    },
    "id":"smoke1"
  }' \
  http://localhost:18790/a2a/rpc
# Expected: task completes synchronously; gateway log shows:
#   POST /a2a/rpc 200
#   POST /webhook/a2a 200
```

### 7.12 Standalone agent health

```bash
curl -s http://localhost:18792/health
# Expected: {"status":"ok","service":"obscura-standalone-agent","port":18792}

# Chat UI accessible in browser:
open http://localhost:18792/

# Over Tailscale:
open https://elliotts-macbook-pro-1.tail91e620.ts.net:18792/
```

### 7.13 ConnectionRegistry connection count

```python
from obscura.integrations.network_gateway.connections import get_registry
r = get_registry()
print(f"Connected WS clients: {r.count}")
```

### 7.14 Telegram webhook smoke test (via Tailscale)

```bash
# Simulate a Telegram update to the Tailscale-exposed endpoint
SECRET=$(python3 -c "import os; print(os.environ.get('TELEGRAM_WEBHOOK_SECRET',''))")
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: $SECRET" \
  -d '{"update_id":1,"message":{"message_id":1,"from":{"id":12345,"first_name":"Test"},"chat":{"id":12345,"type":"private"},"text":"hello"}}' \
  https://elliotts-macbook-pro-1.tail91e620.ts.net/channels/telegram/webhook
# Expected: {"status":"ok"}
# If 503: TELEGRAM_WEBHOOK_SECRET not configured in ~/.obscura/config.toml
```

### 7.15 Agent monitor

```bash
# PID may differ — check the current lock files
ls -la ~/.obscura/sessions/*.lock 2>/dev/null | head -5
```

### 7.13 Circuit breaker state

If `OpenClawBridge` returns `circuit_open` errors, the breaker tripped after repeated OpenClaw failures. Wait for the recovery timeout (default: see `ObscuraConfig.circuit_breaker_recovery`) or restart the Obscura process to reset.

Check the audit log for recent failures:

```bash
tail -20 ~/.obscura/logs/a2a-bridge.jsonl | python3 -m json.tool
```

---

## Related Documents

- `obscura/integrations/a2a/NETWORK.md` — A2A protocol details, auth flow, peer discovery, secrets management
- `obscura/integrations/network_gateway/config.py` — `GatewayConfig` reference
- `obscura/integrations/network_gateway/tailscale.py` — Tailscale serve helpers
- `obscura/integrations/a2a/openclaw_bridge.py` — `OpenClawBridge` implementation
- `obscura/integrations/a2a/well_known.py` — peer registry
- `obscura/integrations/a2a/token_manager.py` — token resolution and rotation

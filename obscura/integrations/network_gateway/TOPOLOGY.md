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

| Port  | Bound on       | Node         | Protocol(s)                        | Purpose                                         |
|-------|----------------|--------------|------------------------------------|-------------------------------------------------|
| 18789 | 127.0.0.1      | OpenClaw     | HTTP + WebSocket                   | OpenClaw gateway (loopback only)               |
| 18790 | 0.0.0.0        | Obscura      | HTTP (OpenAI-compat, A2A, WS)      | Obscura network gateway — primary inter-node port |
| 8080  | 127.0.0.1      | Obscura      | HTTP (REST, SSE, JSON-RPC)         | Obscura A2A SDK server (original)              |
| 443   | Tailscale VIP  | OpenClaw     | WSS (HTTPS tunnel)                 | `modernizedai.tail91e620.ts.net` remote WS     |
| 443   | Tailscale VIP  | Obscura      | HTTPS (tunnel → 18790)             | `elliotts-macbook-pro-1.tail91e620.ts.net`     |

**Route map within port 18790:**

| Path                          | Method    | Description                            |
|-------------------------------|-----------|----------------------------------------|
| `/v1/chat/completions`        | POST      | OpenAI-compatible completions          |
| `/v1/models`                  | GET       | List Obscura backends as model objects |
| `/v1/chat/ws`                 | WebSocket | Bidirectional streaming chat           |
| `/a2a/rpc`                    | POST      | A2A JSON-RPC                           |
| `/a2a/v1/tasks`               | POST/GET  | A2A REST task management               |
| `/a2a/v1/tasks/streaming`     | GET (SSE) | A2A SSE streaming responses            |
| `/.well-known/agent.json`     | GET       | A2A agent card (public, no auth)       |
| `/health`                     | GET       | Liveness probe (public, no auth)       |

---

## 3. Auth Map

| Token / Secret               | Held by          | Used for                                               | Resolution order                                                                             |
|------------------------------|------------------|--------------------------------------------------------|---------------------------------------------------------------------------------------------|
| `4a30d783...eda8754a`        | Obscura          | Outbound calls from `OpenClawBridge` → OpenClaw :18789 | Hardcoded in `OpenClawBridgeConfig.token`; falls back to `OPENCLAW_TOKEN` env var           |
| `~/.obscura/network-gateway.token` | Obscura    | Inbound auth on Obscura network gateway :18790         | `OBSCURA_NETWORK_TOKEN` env var → `~/.obscura/network-gateway.token` → empty (no auth)     |
| `~/.obscura/a2a-gateway.token` | Obscura        | Inbound auth on Obscura A2A SDK server :8080           | `OBSCURA_A2A_TOKEN` env var → `~/.obscura/a2a-gateway.token`                               |
| `~/.openclaw/openclaw.json` → `gateway.auth.token` | OpenClaw | OpenClaw→Obscura outbound calls :18790 | Read by OpenClaw at startup from its config file                                            |

**OpenClaw→Obscura calls are currently unauthenticated** (see `NETWORK.md` status table). The Obscura network gateway token must be supplied in `~/.openclaw/openclaw.json` under `a2aPeers[].token` once auth enforcement is enabled.

**Middleware stack on port 18790 (outermost → innermost):**

```
SecurityHeaders → GatewayRateLimit (60 req/min/IP) → GatewayBearerAuth → CORS → routes
```

Exempt from auth and rate limiting: `/health`, `/.well-known/`.

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

### 4.7 Remote access via Tailscale

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

### 7.10 Agent monitor

```bash
kill -0 47368 2>/dev/null && echo "monitor alive" || echo "monitor dead"
ls -la ~/.obscura/sessions/*.lock 2>/dev/null | head -5
```

### 7.11 Circuit breaker state

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

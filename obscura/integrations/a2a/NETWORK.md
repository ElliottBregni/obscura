# A2A Network Topology — Obscura ↔ OpenClaw

> **Full two-node topology (ASCII diagram, port map, auth map, message flows, shared state, third-node guide, troubleshooting):**
> [`obscura/integrations/network_gateway/TOPOLOGY.md`](../network_gateway/TOPOLOGY.md)

## Status

| Component | State | Notes |
|-----------|-------|-------|
| Obscura→OpenClaw | ✅ Implemented | `OpenClawBridge` via chat completions |
| OpenClaw→Obscura | ✅ Implemented | `a2aPeers` config patched in `~/.openclaw/openclaw.json` |
| Streaming | ✅ Implemented | `stream_send()` SSE |
| Multi-turn | ✅ Implemented | `OpenClawContext` |
| Auto-init | ✅ Implemented | `ask_openclaw` tool registered on provider startup |
| Auth inbound | ✅ Full | Bearer token enforced on :18790 (`~/.obscura/network-gateway.token`); webhook callbacks verified with HMAC-SHA256 |

---

## Quick Start

```python
import asyncio
from obscura.integrations.a2a.openclaw_bridge import OpenClawBridge, OpenClawBridgeConfig

async def main():
    config = OpenClawBridgeConfig(
        base_url="http://localhost:18789",
        token="<openclaw-bearer-token>",
        model="openclaw/main",
    )
    bridge = OpenClawBridge(config)

    # Single-turn
    task = await bridge.send("Summarise the last git commit in this repo")
    print(task.artifacts[0].parts[0].text)

    # Streaming
    async for chunk in bridge.stream_send("Explain the A2A protocol"):
        print(chunk, end="", flush=True)

asyncio.run(main())
```

---

## Current topology

```
┌────────────────────────────────────────────────────────────────┐
│                        Local machine                           │
│                                                                │
│  ┌──────────────────────────┐    A2A REST/SSE/JSON-RPC         │
│  │  Obscura Network Gateway │◄───────────────────────────────  │
│  │  port 18790 (0.0.0.0)   │  Auth: Bearer token (inbound)   │
│  │                          │  Webhook: HMAC-SHA256 signed     │
│  │  /.well-known/agent.json │                                  │
│  │  /a2a/rpc                │                                  │
│  │  /webhook/a2a  ◄─────────┼── push notification callbacks   │
│  └──────────┬───────────────┘                                  │
│             │  OpenClawBridge                                  │
│             │  POST /v1/chat/completions                       │
│             ▼                                                  │
│  ┌──────────────────────────┐                                  │
│  │  OpenClaw Gateway        │                                  │
│  │  port 18789 (loopback)   │                                  │
│  │  (WS + HTTP)             │                                  │
│  │  model: openclaw/main    │                                  │
│  └──────────────────────────┘                                  │
└────────────────────────────────────────────────────────────────┘
```

Both processes run on the same host.  The communication is loopback — no
external network required for local development.

---

## Auth flow

### Obscura → OpenClaw (outbound)

Every HTTP request from `OpenClawBridge` to the OpenClaw gateway carries:

```
Authorization: Bearer <openclaw_token>
```

The token is set once in `OpenClawBridgeConfig.token` and injected as a
default header on the underlying `httpx.AsyncClient`.

### OpenClaw → Obscura (inbound)

All calls from OpenClaw to the Obscura network gateway (:18790) must carry:

```
Authorization: Bearer <value from ~/.obscura/network-gateway.token>
```

The token is configured in `~/.openclaw/openclaw.json` under `a2aPeers[].token`
and `a2aPeers[].authToken`.  `GatewayBearerAuthMiddleware` validates it with a
timing-safe comparison (`hmac.compare_digest`).

### OpenClaw → Obscura webhook callbacks (HMAC)

Push notification callbacks fired by Obscura back to itself at
`POST http://localhost:18790/webhook/a2a` are HMAC-SHA256 signed:

```
X-Webhook-Signature: sha256=<hex-digest>
```

The shared secret is in `~/.obscura/network-gateway-webhook.secret`.  The
gateway verifies the signature before processing the payload.  Requests with
missing or invalid signatures are rejected with `401 {"error":"invalid_signature"}`.

---

## Message flow

### Obscura → OpenClaw (single-turn text)

```
Caller                  OpenClawBridge          OpenClaw Gateway
  │                          │                        │
  │  bridge.send(text)       │                        │
  │─────────────────────────►│                        │
  │                          │  POST /v1/chat/        │
  │                          │  completions           │
  │                          │───────────────────────►│
  │                          │                        │ inference
  │                          │   {choices:[{message}]}│
  │                          │◄───────────────────────│
  │                          │                        │
  │   A2ATask (completed)    │                        │
  │◄─────────────────────────│                        │
```

1. `OpenClawBridge.send(text)` (or `send_a2a_message(A2AMessage)`) builds an
   OpenAI-compatible payload: `{"model": "openclaw/main", "messages": [...]}`.
2. The bridge POSTs to `http://localhost:18789/v1/chat/completions`.
3. OpenClaw runs inference and returns `{"choices": [{"message": {"content": "..."}}]}`.
4. The bridge wraps the reply in an `A2ATask` with state `completed` and one
   `Artifact` whose first part is the reply `TextPart`.

### OpenClaw → Obscura (A2A REST)

OpenClaw sends A2A JSON-RPC calls to the Obscura network gateway on :18790.
The `a2aPeers` entry in `~/.openclaw/openclaw.json` points to this endpoint
and carries the bearer token automatically:

```
OpenClaw              Obscura Network GW (port 18790)
  │                          │
  │  POST /a2a/rpc           │
  │  Authorization: Bearer X │
  │  {"method":"message/send"│
  │  ,"params":{...}}        │
  │─────────────────────────►│
  │                          │ agent loop execution (Claude)
  │   {"result": Task}       │
  │◄─────────────────────────│
  │                          │
  │  POST /webhook/a2a       │ ← push notification (HMAC signed)
  │  X-Webhook-Signature:... │
  │◄─────────────────────────│
```

---

## Transport selection rationale

| Direction         | Transport          | Reason                                          |
|-------------------|--------------------|-------------------------------------------------|
| Obscura→OpenClaw  | HTTP POST (REST)   | OpenClaw only exposes `/v1/chat/completions`    |
| Obscura→OpenClaw  | No SSE yet         | OpenClaw's streaming support TBD                |
| OpenClaw→Obscura  | A2A REST (JSON-RPC)| Simple, no long-lived connections needed        |
| OpenClaw→Obscura  | A2A SSE            | Available for streaming responses               |

REST is preferred over JSON-RPC for simple single-call interactions because
the round-trip is a plain HTTP request/response — easier to debug with curl.
SSE is the right choice for streaming intermediate results; Obscura's
`/a2a/v1/tasks/streaming` endpoint is already wired.

---

## Peer discovery

### Well-known registry (`well_known.py`)

`DEFAULT_REGISTRY` ships with two pre-registered peers:

| Name           | URL                        | Notes                            |
|----------------|----------------------------|----------------------------------|
| `openclaw`     | `http://localhost:18789`   | OpenClaw gateway (chat completions) |
| `obscura_local`| `http://localhost:8080`    | Local Obscura A2A endpoint       |

OpenClaw does not serve `/.well-known/agent.json` — `discover_all()` will
log a warning and skip it.  This is expected behavior.

### From config

```python
registry = WellKnownAgentRegistry.from_config({
    "well_known_agents": [
        {"name": "openclaw", "url": "http://localhost:18789"},
    ]
})
```

### Dynamic registration

```python
from obscura.integrations.a2a.well_known import DEFAULT_REGISTRY, WellKnownAgent

DEFAULT_REGISTRY.register(WellKnownAgent(
    name="my_peer",
    url="http://peer.example.com:9000",
    auth_token="s3cr3t",
))
```

---

## Adding new peers

1. **Register in `DEFAULT_REGISTRY`** (for built-in peers):
   Add a `WellKnownAgent` block in `well_known.py` near the `openclaw` entry.

2. **Config-file peers** (for operator-added peers):
   Add entries to `ServerConfig.well_known_agents` in the YAML/TOML that
   drives `build_standalone_server()`.

3. **Custom bridge** (for peers that don't speak A2A):
   Subclass or instantiate `OpenClawBridge` with the peer's URL and token.
   The bridge pattern is protocol-agnostic — swap `/v1/chat/completions` for
   any HTTP endpoint by overriding `_build_completions_payload` and
   `_parse_completions_response`.

---

## Secrets Management

Token resolution is centralised in
`obscura.integrations.a2a.token_manager.A2ATokenManager`.

### Environment variables (production)

Set these in your shell or in `~/.obscura/.env` before starting Obscura:

| Variable                  | Used for                                             |
|---------------------------|------------------------------------------------------|
| `OPENCLAW_TOKEN`          | Bearer token sent to OpenClaw gateway (outbound)     |
| `OBSCURA_NETWORK_TOKEN`   | Bearer token accepted by Obscura network gateway :18790 |
| `OBSCURA_WEBHOOK_SECRET`  | HMAC-SHA256 key for push notification signing/verifying |

A template is provided at `~/.obscura/.env.a2a.example`.

### Token files (local dev fallback)

When the env vars are absent, the manager falls back to on-disk files:

| Secret                    | File                                                 |
|---------------------------|------------------------------------------------------|
| `OPENCLAW_TOKEN`          | `~/.openclaw/openclaw.json` → `a2aPeers[].token`    |
| `OBSCURA_NETWORK_TOKEN`   | `~/.obscura/network-gateway.token`                   |
| `OBSCURA_WEBHOOK_SECRET`  | `~/.obscura/network-gateway-webhook.secret`          |

### Token rotation

Use `A2ATokenManager.rotate_a2a_token()` to generate a new 64-hex-character
Obscura A2A token and persist it automatically:

```python
from obscura.integrations.a2a.token_manager import A2ATokenManager

new_token = A2ATokenManager().rotate_a2a_token()
print(f"New token: {new_token}")
# Update OBSCURA_A2A_TOKEN in ~/.obscura/.env and restart the A2A server.
```

---

## Ports reference

| Service                  | Port  | Protocol                        |
|--------------------------|-------|---------------------------------|
| Obscura network gateway  | 18790 | HTTP (OpenAI-compat, A2A, WS)  |
| OpenClaw gateway         | 18789 | HTTP + WebSocket                |
| Obscura A2A SDK server   | 8080  | HTTP (REST, SSE, JSON-RPC) — legacy internal; primary is 18790 |

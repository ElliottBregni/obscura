# A2A Network Topology — Obscura ↔ OpenClaw

## Current topology

```
┌────────────────────────────────────────────────────────┐
│                  Local machine                         │
│                                                        │
│  ┌─────────────────────┐    REST/SSE/JSON-RPC          │
│  │  Obscura A2A Server │◄────────────────────────────  │
│  │  port 8080          │  (inbound A2A from any peer)  │
│  │                     │                               │
│  │  /.well-known/      │                               │
│  │    agent.json       │                               │
│  └────────┬────────────┘                               │
│           │  OpenClawBridge                            │
│           │  POST /v1/chat/completions                 │
│           ▼                                            │
│  ┌─────────────────────┐                               │
│  │  OpenClaw Gateway   │                               │
│  │  port 18789         │                               │
│  │  (WS + HTTP)        │                               │
│  │  model: openclaw/main                               │
│  └─────────────────────┘                               │
└────────────────────────────────────────────────────────┘
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

### OpenClaw → Obscura (inbound, future)

When OpenClaw needs to call back into Obscura via A2A REST, it must send:

```
Authorization: Bearer <obscura_token>
```

The Obscura A2A server validates this via `APIKeyAuthMiddleware` (configured
through `ServerConfig.bearer_tokens`).  The `a2a-gateway.yaml` config stores
the accepted token in `~/.obscura/a2a-gateway.token`.

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

### OpenClaw → Obscura (A2A REST, future)

OpenClaw does not currently speak A2A natively.  When support is added:

```
OpenClaw              Obscura A2A Server (port 8080)
  │                          │
  │  POST /a2a/rpc           │
  │  {"method":"message/send"│
  │  ,"params":{...}}        │
  │─────────────────────────►│
  │                          │ task execution (agent loop)
  │   {"result": Task}       │
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

## Ports reference

| Service         | Port  | Protocol                   |
|-----------------|-------|----------------------------|
| Obscura A2A     | 8080  | HTTP (REST, SSE, JSON-RPC) |
| OpenClaw gateway| 18789 | HTTP + WebSocket           |
| Obscura A2A (gateway daemon) | 18791 | HTTP (see a2a-gateway.yaml) |

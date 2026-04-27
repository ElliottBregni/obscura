# Obscura — Setup Guide

> Full offline (local dev) and online (production) setup instructions.

---

## Architecture

```
┌─────────────────────────────────────┐
│  Claude Code / Claude Desktop        │
│  MCP Client (stdio)                  │
└──────────────┬──────────────────────┘
               │ tool calls
┌──────────────▼──────────────────────┐
│  Obscura MCP Server                  │
│  obscura/mcp_server.py (stdio)       │
└──────────────┬──────────────────────┘
               │ HTTP → localhost:8080
┌──────────────▼──────────────────────┐
│  Obscura API (FastAPI)               │
│  Port 8080 · /api/v1/*               │
│  Auth: X-API-Key or Bearer JWT       │
└──────────────┬──────────────────────┘
               │
       ┌───────┴────────┐
       │                │
  ┌────▼────┐    ┌──────▼──────┐
  │ Qdrant  │    │ Web UI       │
  │ :6333   │    │ Vite :5173   │
  │ vectors │    │ React admin  │
  └─────────┘    └─────────────┘
```

---

## Mode 1 — Offline (Local Dev)

Everything runs on your machine. No cloud services required (Qdrant optional).

### Prerequisites

```bash
# Python venv already at:
/Users/elliottbregni/dev/obscura-main/.venv

# Node.js ≥18
node --version

# Qdrant (for vector memory) — Docker:
docker run -p 6333:6333 qdrant/qdrant
# Or skip: set OBSCURA_VECTOR_BACKEND=none
```

### 1. Start the Obscura API

```bash
cd /Users/elliottbregni/dev/obscura-main

export OBSCURA_API_KEYS="obscura_LcTDYtNivCUsvdn4H9gh0O2lj-skhDVrmqGE9Olgkdw:admin:agent:read,agent:copilot,sessions:manage,sync:write"
export OBSCURA_PORT=8080
export OBSCURA_QDRANT_URL=http://localhost:6333
export OBSCURA_VECTOR_BACKEND=qdrant

.venv/bin/python -m obscura.main
```

Verify: `curl http://localhost:8080/health` → `{"status":"ok"}`

### 2. Web UI `.env` (already created at `~/dev/obscura-web-ui/.env`)

```dotenv
VITE_DEV_API_KEY=obscura_LcTDYtNivCUsvdn4H9gh0O2lj-skhDVrmqGE9Olgkdw
VITE_FORCE_DEV_API_KEY=true
VITE_PROXY_TARGET=http://localhost:8080
VITE_WS_PROXY_TARGET=ws://localhost:8080
```

`VITE_FORCE_DEV_API_KEY=true` injects the API key on every request and bypasses the login screen entirely.

### 3. Start the Web UI

```bash
cd ~/dev/obscura-web-ui
cp .env.example .env  # set VITE_API_URL=http://localhost:8080
npm install      # first time only
npm run dev      # → http://localhost:5173
```

### 4. MCP Server for Claude Code

`~/.claude/claude_desktop_config.json` is already patched. Restart Claude Code once and all `obscura.*` MCP tools will work.

Key config block:
```json
"obscura": {
  "command": "/Users/elliottbregni/dev/obscura-main/.venv/bin/python",
  "args": ["-m", "obscura.mcp_server", "--transport", "stdio"],
  "env": {
    "OBSCURA_API_KEYS": "obscura_LcTDYtNivCUsvdn4H9gh0O2lj-skhDVrmqGE9Olgkdw:admin:agent:read,agent:copilot",
    "OBSCURA_QDRANT_URL": "http://localhost:6333",
    "OBSCURA_QDRANT_MODE": "cloud",
    "OBSCURA_VECTOR_BACKEND": "qdrant",
    "OBSCURA_PORT": "8080"
  }
}
```

> ⚠️ Restart Claude Code after any config change — the MCP server process does NOT inherit your shell env.

---

## Mode 2 — Online (Production)

### 1. Server Environment

```bash
OBSCURA_ENV=prod
OBSCURA_PORT=8080
OBSCURA_JWKS_STRICT=true
OBSCURA_AUTH_MODE=oauth_first

# Replace with your production key
OBSCURA_API_KEYS="<prod-key>:admin:agent:read,agent:copilot,sessions:manage,sync:write"

OBSCURA_VECTOR_BACKEND=qdrant
OBSCURA_QDRANT_URL=http://qdrant:6333

OBSCURA_LOG_LEVEL=INFO
OBSCURA_LOG_FORMAT=json
```

Or use the included env template:
```bash
cp config/env/prod.env .env
# Edit OBSCURA_API_KEYS with your real key
docker compose up -d
```

### 2. Build Frontend

```bash
cd ~/dev/obscura-web-ui

# Point frontend at the deployed API
echo 'VITE_API_URL=https://obscura.yourdomain.com' > .env.production

npm run build
# Output: ~/dev/obscura-web-ui/dist/  → serve as static files
```

### 3. Nginx Config (reverse proxy)

```nginx
server {
    server_name obscura.yourdomain.com;

    location / {
        root /var/www/obscura/dist;
        try_files $uri $uri/ /index.html;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
    }
    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    location /mcp/  { proxy_pass http://127.0.0.1:8080; }
    location /a2a/  { proxy_pass http://127.0.0.1:8080; }
}
```

---

## Environment Variables

### Backend

| Variable | Default | Description |
|---|---|---|
| `OBSCURA_PORT` | `8080` | API listen port |
| `OBSCURA_API_KEYS` | — | `token:user:role1,role2` (multiple separated by `;`) |
| `OBSCURA_JWKS_STRICT` | `false` | Require valid JWKS for JWT auth |
| `OBSCURA_AUTH_MODE` | `oauth_first` | `oauth_first` or `api_key_only` |
| `OBSCURA_VECTOR_BACKEND` | `qdrant` | `qdrant` or `none` |
| `OBSCURA_QDRANT_URL` | — | Qdrant server URL |
| `OBSCURA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `OBSCURA_A2A_ENABLED` | `true` | Enable Agent-to-Agent protocol |
| `OBSCURA_A2A_REDIS_URL` | — | Redis URL for A2A pub/sub |

### Frontend (Vite)

| Variable | Default | Description |
|---|---|---|
| `VITE_DEV_API_KEY` | — | API key injected in dev requests |
| `VITE_FORCE_DEV_API_KEY` | `false` | `true` = auto-auth, bypass login screen |
| `VITE_PROXY_TARGET` | `http://localhost:8080` | Vite HTTP proxy target |
| `VITE_WS_PROXY_TARGET` | `ws://localhost:8080` | Vite WebSocket proxy target |
| `VITE_API_URL` | `''` | Explicit API URL for production builds |

---

## Web UI Routes

| Path | Feature | Admin Only |
|---|---|---|
| `/` | Dashboard | |
| `/agents` | Agent list + status | |
| `/agents/spawn` | Spawn wizard | |
| `/agents/templates` | Agent templates | |
| `/agents/groups` | Agent groups | |
| `/agents/:id` | Agent detail | |
| `/agents/:id/chat` | Chat with agent | |
| `/memory` | Key-value + vector memory | |
| `/workflows` | DAG workflows | |
| `/approvals` | Tool approval queue | |
| `/webhooks` | Webhook manager | |
| `/audit` | Audit log | |
| `/sessions` | Session list | |
| `/admin` | Admin panel | ✅ |
| `/admin/rate-limits` | Rate limit config | ✅ |
| `/admin/capabilities` | Capability config | ✅ |
| `/admin/metrics` | Metrics dashboard | ✅ |
| `/health` | Service health | |
| `/mcp` | MCP server status | |
| `/a2a` | Agent-to-Agent | |

---

## Troubleshooting

**401 on all `obscura.*` MCP tools**
The MCP server subprocess does not inherit shell env vars. Ensure `OBSCURA_API_KEYS` is in the `"env"` block of `~/.claude/claude_desktop_config.json`. Restart Claude Code after changing.

**Web UI shows login screen despite `VITE_FORCE_DEV_API_KEY=true`**
Check that `~/dev/obscura-web-ui/.env` exists with both `VITE_DEV_API_KEY` and `VITE_FORCE_DEV_API_KEY=true`. Restart `npm run dev` — Vite must restart to pick up `.env` changes.

**API returns 404 on `/api/sessions`**
All routes are prefixed `/api/v1/` — use `/api/v1/sessions`, `/api/v1/agents`, etc.

**WebSocket disconnected in the UI**
Ensure `VITE_WS_PROXY_TARGET=ws://localhost:8080` is set and the API server is running. The WS endpoint is `/ws/` (proxied by Vite in dev).

**Qdrant connection errors**
Start Qdrant: `docker run -p 6333:6333 qdrant/qdrant` or set `OBSCURA_VECTOR_BACKEND=none` to disable.

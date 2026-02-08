# 🔌 Obscura API Development Plan

> Roadmap for extending the Obscura API beyond core functionality.

---

## Current State

### ✅ Existing Endpoints

| Category | Endpoints | Status |
|----------|-----------|--------|
| Health | `GET /health`, `GET /ready` | ✅ |
| Agents | `POST /api/v1/agents`, `GET /api/v1/agents`, `GET /api/v1/agents/{id}`, `DELETE /api/v1/agents/{id}`, `POST /api/v1/agents/{id}/run` | ✅ |
| Memory | `GET /api/v1/memory`, `GET /api/v1/memory/{ns}/{key}`, `POST /api/v1/memory/{ns}/{key}`, `DELETE /api/v1/memory/{ns}/{key}` | ✅ |
| Vector | `POST /api/v1/vector-memory/{ns}/{key}`, `GET /api/v1/vector-memory/search` | ✅ |
| Send | `POST /api/v1/send`, `POST /api/v1/stream` | ✅ |
| Sync | `POST /api/v1/sync` | ✅ |

### 🔐 Auth Options

| Method | Implementation | Status |
|--------|----------------|--------|
| Disabled | `OBSCURA_AUTH_ENABLED=false` | ✅ |
| API Key | `X-API-Key` header | ✅ |
| JWT | Via Zitadel | ✅ |

---

## Phase 1: Enhanced Agent Management (Week 1)

### 1.1 Agent Bulk Operations
```http
POST /api/v1/agents/bulk
{
  "agents": [
    {"name": "agent-1", "model": "claude"},
    {"name": "agent-2", "model": "copilot"}
  ]
}

DELETE /api/v1/agents/bulk
{
  "agent_ids": ["agent-1", "agent-2"]
}
```

### 1.2 Agent Templates
```http
POST /api/v1/agent-templates
{
  "name": "code-reviewer",
  "model": "claude",
  "system_prompt": "Review code for security...",
  "timeout_seconds": 300
}

POST /api/v1/agents/from-template
{
  "template_id": "template-123",
  "name": "my-reviewer"
}
```

### 1.3 Agent Tags & Filtering
```http
GET /api/v1/agents?tags=production,critical&status=running

POST /api/v1/agents/{id}/tags
{
  "tags": ["production", "critical"]
}
```

---

## Phase 2: Advanced Memory Features (Week 2)

### 2.1 Memory Namespaces Management
```http
GET /api/v1/memory/namespaces

POST /api/v1/memory/namespaces
{
  "name": "project-alpha",
  "description": "Project Alpha context",
  "ttl_days": 30
}

DELETE /api/v1/memory/namespaces/{name}
```

### 2.2 Memory Transactions
```http
POST /api/v1/memory/transaction
{
  "operations": [
    {"op": "set", "namespace": "session", "key": "a", "value": 1},
    {"op": "delete", "namespace": "session", "key": "b"},
    {"op": "get", "namespace": "session", "key": "c"}
  ]
}
```

### 2.3 Memory Import/Export
```http
GET /api/v1/memory/export?namespace=project-alpha
# Returns JSON dump

POST /api/v1/memory/import
Content-Type: multipart/form-data
# Upload JSON file
```

### 2.4 Memory Analytics
```http
GET /api/v1/memory/stats
{
  "total_entries": 1500,
  "total_size_bytes": 5242880,
  "namespaces": {
    "session": {"count": 500, "size": 1048576},
    "project": {"count": 1000, "size": 4194304}
  },
  "oldest_entry": "2026-01-01T00:00:00Z",
  "newest_entry": "2026-02-08T00:00:00Z"
}
```

---

## Phase 3: WebSocket & Real-time (Week 3)

### 3.1 Agent WebSocket
```http
WS /ws/agents/{agent_id}

# Client -> Server
{
  "type": "run",
  "prompt": "Hello!",
  "context": {}
}

# Server -> Client (streaming)
{
  "type": "chunk",
  "content": "Hello",
  "timestamp": "2026-02-08T00:00:00Z"
}
{
  "type": "done",
  "final_response": "Hello! How can I help?"
}
```

### 3.2 Broadcast WebSocket
```http
WS /ws/broadcast

# Server -> All clients
{
  "type": "agent_spawned",
  "agent_id": "agent-123",
  "timestamp": "2026-02-08T00:00:00Z"
}

{
  "type": "agent_status_changed",
  "agent_id": "agent-123",
  "status": "completed",
  "timestamp": "2026-02-08T00:00:00Z"
}
```

### 3.3 Memory Watch
```http
WS /ws/memory/{namespace}

# Server -> Client when memory changes
{
  "type": "memory_set",
  "key": "context",
  "timestamp": "2026-02-08T00:00:00Z"
}
```

---

## Phase 4: Multi-Agent Orchestration (Week 4)

### 4.1 Agent Groups
```http
POST /api/v1/agent-groups
{
  "name": "review-team",
  "agents": ["agent-1", "agent-2", "agent-3"]
}

POST /api/v1/agent-groups/{group_id}/broadcast
{
  "message": "Review this code",
  "context": {"pr_url": "..."}
}
```

### 4.2 Workflows
```http
POST /api/v1/workflows
{
  "name": "code-review-pipeline",
  "steps": [
    {
      "name": "security-review",
      "agent_template": "security-reviewer",
      "input": "{{original_code}}"
    },
    {
      "name": "performance-review",
      "agent_template": "performance-reviewer",
      "input": "{{original_code}}",
      "depends_on": ["security-review"]
    },
    {
      "name": "summarize",
      "agent_template": "summarizer",
      "input": "{{security-review.output}} + {{performance-review.output}}",
      "depends_on": ["security-review", "performance-review"]
    }
  ]
}

POST /api/v1/workflows/{id}/execute
{
  "inputs": {
    "original_code": "def foo(): ..."
  }
}
```

### 4.3 Agent-to-Agent Messaging
```http
POST /api/v1/agents/{from_agent}/send/{to_agent}
{
  "message": "Can you review this?",
  "context": {"file": "main.py"}
}

GET /api/v1/agents/{agent_id}/messages
```

---

## Phase 5: Integrations & Tools (Week 5)

### 5.1 MCP (Model Context Protocol) Integration
```http
GET /api/v1/mcp/servers

POST /api/v1/mcp/servers
{
  "name": "github",
  "url": "http://localhost:3001/sse",
  "tools": ["create_issue", "get_pr"]
}

POST /api/v1/agents/{id}/tools/enable
{
  "tool_name": "github/create_issue",
  "config": {"repo": "my-org/my-repo"}
}
```

### 5.2 External Memory Sources
```http
POST /api/v1/memory/sources/github
{
  "repo": "my-org/my-repo",
  "sync_issues": true,
  "sync_prs": true
}

GET /api/v1/memory/sources
```

### 5.3 Webhooks
```http
POST /api/v1/webhooks
{
  "url": "https://my-app.com/webhook",
  "events": ["agent.completed", "agent.failed"],
  "secret": "webhook-secret"
}
```

---

## Phase 6: Admin & Observability (Week 6)

### 6.1 Audit Logs
```http
GET /api/v1/audit/logs
?start=2026-02-01T00:00:00Z
&end=2026-02-08T00:00:00Z
&user_id=user-123
&resource=agent:agent-456

Response:
{
  "logs": [
    {
      "timestamp": "2026-02-08T00:00:00Z",
      "user_id": "user-123",
      "action": "agent.spawn",
      "resource": "agent:agent-456",
      "outcome": "success",
      "details": {"model": "claude"}
    }
  ]
}
```

### 6.2 Metrics
```http
GET /api/v1/metrics

GET /api/v1/metrics/agents/{id}
{
  "total_runs": 150,
  "total_tokens": 50000,
  "average_latency_ms": 2500,
  "success_rate": 0.98
}
```

### 6.3 Rate Limits
```http
GET /api/v1/rate-limits
{
  "requests_per_minute": 100,
  "concurrent_agents": 10,
  "memory_quota_mb": 1024
}

POST /api/v1/rate-limits
{
  "api_key": "key-123",
  "requests_per_minute": 200,
  "concurrent_agents": 20
}
```

---

## Phase 7: CLI Enhancements (Ongoing)

### 7.1 Config Profiles
```bash
obscura config set-profile production
obscura config set --profile=production url https://api.obscura.io
obscura config set --profile=production token $PROD_TOKEN

obscura --profile=production agent list
```

### 7.2 Interactive Mode
```bash
obscura interactive

> agent spawn --name helper --model claude
> agent run helper "Hello!"
> memory set session context '{"topic": "python"}'
```

### 7.3 Batch Operations
```bash
obscura agents spawn-from-file agents.yaml
obscura agents stop-all --filter status=completed
obscura memory export --namespace=project > backup.json
```

---

## Implementation Priority

### Must Have (P0)
- [ ] WebSocket streaming for real-time responses
- [ ] Agent templates
- [ ] Memory analytics
- [ ] Audit logging

### Should Have (P1)
- [ ] Bulk operations
- [ ] Memory import/export
- [ ] Webhooks
- [ ] Rate limiting

### Nice to Have (P2)
- [ ] MCP integration
- [ ] Workflows
- [ ] Agent groups
- [ ] External memory sources

---

## Technical Considerations

### 1. Backwards Compatibility
- All new endpoints go under `/api/v2/` or use feature flags
- Deprecation warnings for old endpoints
- 6-month migration period

### 2. Performance
- Pagination for list endpoints (`?limit=50&offset=100`)
- Cursor-based pagination for large datasets
- ETags for caching

### 3. Security
- Rate limiting per API key
- Request size limits
- Input validation/sanitization
- Audit all admin operations

### 4. Documentation
- OpenAPI/Swagger spec auto-generated
- Postman collection
- SDK updates (Python, JS)

---

## Success Metrics

| Metric | Target |
|--------|--------|
| API Response Time (p99) | < 100ms |
| WebSocket Latency | < 50ms |
| Concurrent Agents | 100+ |
| Memory Entries | 1M+ |
| Uptime | 99.9% |

---

## Timeline Summary

| Phase | Duration | Deliverables |
|-------|----------|--------------|
| 1: Enhanced Agents | Week 1 | Bulk ops, templates, tags |
| 2: Advanced Memory | Week 2 | Namespaces, transactions, analytics |
| 3: WebSocket | Week 3 | Real-time streaming, broadcast |
| 4: Orchestration | Week 4 | Groups, workflows, messaging |
| 5: Integrations | Week 5 | MCP, external sources, webhooks |
| 6: Admin/Observability | Week 6 | Audit logs, metrics, rate limits |

**Total: 6 weeks for full feature set**

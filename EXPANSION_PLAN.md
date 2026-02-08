# 🚀 Obscura Full-Stack Expansion Plan

> React Admin Portal + MCP/Skills + Heartbeat System + QA/UAT

**Status:** Planning Phase  
**Scope:** Full-stack transformation  
**Target:** Production-ready v1.0

---

## 📋 Executive Summary

Expand Obscura from a backend API with basic web-ui to a **complete platform** with:
- 🎨 **React Admin Portal** - Modern, real-time management interface
- 🔌 **MCP Integration** - Full Model Context Protocol support
- 🧩 **Skills System** - Pluggable agent capabilities
- 💓 **Heartbeat Monitoring** - Agent health & liveness detection
- ✅ **QA & UAT** - Comprehensive testing & validation

---

## 🏗️ Phase Overview

| Phase | Component | Duration | Lead Agent | Dependencies |
|-------|-----------|----------|------------|--------------|
| 1 | Backend MCP Foundation | 3 days | Agent A | None |
| 2 | Skills Framework | 3 days | Agent B | Phase 1 |
| 3 | Heartbeat System | 2 days | Agent C | None |
| 4 | React Admin Portal | 5 days | Agent D | Phase 1-3 |
| 5 | Integration & API Bridge | 3 days | Agent E | Phase 1-4 |
| 6 | QA Phase | 3 days | All Agents | Phase 1-5 |
| 7 | UAT Phase | 2 days | All Agents | Phase 6 |

**Total Timeline:** 21 days (~3 weeks)

---

## 🔌 Phase 1: MCP (Model Context Protocol) Foundation

### 1.1 MCP Server Architecture

```python
# sdk/mcp/
├── __init__.py
├── server.py          # MCP server implementation
├── client.py          # MCP client for connecting to external servers
├── types.py           # MCP protocol types
├── tools.py           # Tool conversion between MCP <-> Obscura
├── resources.py       # Resource management
└── prompts.py         # Prompt templates
```

### 1.2 MCP Server Implementation

**FastMCP-based server for Obscura:**

```python
# sdk/mcp/server.py
from mcp.server import FastMCP

mcp = FastMCP("obscura")

@mcp.tool()
async def list_agents() -> list[dict]:
    """List all active agents"""
    ...

@mcp.tool()
async def spawn_agent(name: str, model: str, prompt: str) -> dict:
    """Spawn a new agent"""
    ...

@mcp.resource("memory://{namespace}/{key}")
async def get_memory(namespace: str, key: str) -> str:
    """Retrieve memory value"""
    ...

@mcp.prompt()
def agent_task_prompt(task: str) -> str:
    """Template for agent tasks"""
    return f"Execute this task: {task}"
```

### 1.3 MCP Client Integration

**Connect to external MCP servers:**

```python
# sdk/mcp/client.py
class MCPClient:
    """Client for connecting to MCP servers (e.g., GitHub, Filesystem)"""
    
    async def connect_stdio(self, command: str, args: list[str]) -> MCPSession
    async def connect_sse(self, url: str) -> MCPSession
    async def list_tools(self) -> list[MCPTool]
    async def call_tool(self, name: str, arguments: dict) -> ToolResult
    async def read_resource(self, uri: str) -> ResourceContent
```

### 1.4 MCP-Enabled Agent Backend

```python
# sdk/backends/mcp_backend.py
class MCPBackend(AgentBackend):
    """Backend that uses MCP tools/resources"""
    
    def __init__(self, mcp_servers: list[MCPServerConfig]):
        self.sessions = []
        for server in mcp_servers:
            session = await MCPClient.connect(server)
            self.sessions.append(session)
    
    async def complete(self, messages: list[Message]) -> Completion:
        # Aggregate tools from all MCP servers
        tools = []
        for session in self.sessions:
            tools.extend(await session.list_tools())
        
        # Call LLM with available tools
        ...
```

### 1.5 Tasks

- [ ] Create `sdk/mcp/` module structure
- [ ] Implement MCP server using FastMCP
- [ ] Expose Obscura API as MCP tools
- [ ] Implement MCP client for external servers
- [ ] Create MCP backend for agents
- [ ] Add MCP configuration to agent spawn API
- [ ] Write tests for MCP integration (20+ tests)

### Deliverables
- [ ] `sdk/mcp/` module with full MCP support
- [ ] Obscura MCP server running on stdio/SSE
- [ ] MCP client for external servers
- [ ] Test suite: 20+ passing tests

---

## 🧩 Phase 2: Skills Framework

### 2.1 Skills Architecture

```python
# sdk/skills/
├── __init__.py
├── base.py           # Base skill class
├── registry.py       # Skill registry & discovery
├── loader.py         # Dynamic skill loading
├── builtin/          # Built-in skills
│   ├── __init__.py
│   ├── web_search.py
│   ├── file_system.py
│   ├── code_execution.py
│   └── git.py
└── marketplace.py    # Skill marketplace (future)
```

### 2.2 Skill Base Class

```python
# sdk/skills/base.py
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

@dataclass
class SkillCapability:
    name: str
    description: str
    parameters: dict[str, type]
    returns: type

@runtime_checkable
class Skill(Protocol):
    """Base protocol for all skills"""
    
    name: str
    version: str
    description: str
    capabilities: list[SkillCapability]
    
    async def initialize(self, config: dict) -> None:
        """Initialize skill with configuration"""
        ...
    
    async def execute(self, capability: str, params: dict) -> Any:
        """Execute a capability"""
        ...
    
    async def health_check(self) -> bool:
        """Check if skill is healthy"""
        ...
    
    async def shutdown(self) -> None:
        """Cleanup resources"""
        ...
```

### 2.3 Built-in Skills

**Web Search Skill:**
```python
# sdk/skills/builtin/web_search.py
class WebSearchSkill:
    name = "web_search"
    version = "1.0.0"
    description = "Search the web using various providers"
    
    capabilities = [
        SkillCapability("search", "Perform web search", {"query": str}, SearchResult),
        SkillCapability("news", "Get recent news", {"topic": str}, list[NewsItem]),
    ]
    
    async def search(self, query: str) -> SearchResult:
        # Use Brave, Google, or DuckDuckGo
        ...
```

**File System Skill:**
```python
# sdk/skills/builtin/file_system.py
class FileSystemSkill:
    name = "file_system"
    version = "1.0.0"
    description = "Read and write files"
    
    capabilities = [
        SkillCapability("read", "Read file contents", {"path": str}, str),
        SkillCapability("write", "Write file contents", {"path": str, "content": str}, bool),
        SkillCapability("list", "List directory contents", {"path": str}, list[FileInfo]),
    ]
```

**Code Execution Skill:**
```python
# sdk/skills/builtin/code_execution.py
class CodeExecutionSkill:
    name = "code_execution"
    version = "1.0.0"
    description = "Execute code in sandboxed environment"
    
    capabilities = [
        SkillCapability("python", "Run Python code", {"code": str}, ExecutionResult),
        SkillCapability("shell", "Run shell commands", {"command": str}, ExecutionResult),
    ]
```

### 2.4 Skill Registry & Discovery

```python
# sdk/skills/registry.py
class SkillRegistry:
    """Registry for managing skills"""
    
    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self._capabilities: dict[str, SkillCapability] = {}
    
    def register(self, skill: Skill) -> None:
        """Register a skill"""
        self._skills[skill.name] = skill
        for cap in skill.capabilities:
            self._capabilities[f"{skill.name}.{cap.name}"] = cap
    
    def discover(self, query: str) -> list[SkillCapability]:
        """Search capabilities by query"""
        ...
    
    async def execute(self, capability_path: str, params: dict) -> Any:
        """Execute a capability by path (skill.capability)"""
        skill_name, cap_name = capability_path.split(".")
        skill = self._skills[skill_name]
        return await skill.execute(cap_name, params)
```

### 2.5 Skills API Endpoints

```
GET  /api/v1/skills                 # List available skills
GET  /api/v1/skills/{name}          # Get skill details
GET  /api/v1/skills/{name}/health   # Check skill health
POST /api/v1/skills/{name}/execute  # Execute skill capability
POST /api/v1/skills/discover        # Discover capabilities by query
```

### 2.6 Tasks

- [ ] Create `sdk/skills/` module structure
- [ ] Implement Skill base class and registry
- [ ] Build Web Search skill with Brave API
- [ ] Build File System skill
- [ ] Build Code Execution skill (sandboxed)
- [ ] Build Git skill
- [ ] Add skills to agent spawn API
- [ ] Create skills admin endpoints
- [ ] Write tests for skills (25+ tests)

### Deliverables
- [ ] 4+ built-in skills working
- [ ] Skill registry with discovery
- [ ] Skills API endpoints
- [ ] Test suite: 25+ passing tests

---

## 💓 Phase 3: Heartbeat System

### 3.1 Heartbeat Architecture

```python
# sdk/heartbeat/
├── __init__.py
├── monitor.py        # Heartbeat monitoring service
├── health.py         # Health check definitions
├── alerts.py         # Alert system
└── store.py          # Heartbeat data storage
```

### 3.2 Heartbeat Protocol

```python
# sdk/heartbeat/types.py
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class HealthStatus(Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

@dataclass
class Heartbeat:
    agent_id: str
    timestamp: datetime
    status: HealthStatus
    metrics: dict[str, Any]  # CPU, memory, queue_depth, etc.
    message: str | None
    ttl: int  # Time-to-live in seconds

@dataclass
class HealthCheck:
    name: str
    check_fn: Callable[[], HealthStatus]
    interval: int  # seconds
    timeout: int   # seconds
```

### 3.3 Heartbeat Monitor

```python
# sdk/heartbeat/monitor.py
class HeartbeatMonitor:
    """Monitors agent health via heartbeats"""
    
    def __init__(self, store: HeartbeatStore):
        self.store = store
        self.checks: dict[str, HealthCheck] = {}
        self._running = False
    
    async def start(self):
        """Start monitoring loop"""
        self._running = True
        while self._running:
            await self._check_all_agents()
            await asyncio.sleep(10)
    
    async def register_agent(self, agent_id: str, expected_interval: int = 30):
        """Register an agent for monitoring"""
        await self.store.register(agent_id, expected_interval)
    
    async def record_heartbeat(self, heartbeat: Heartbeat):
        """Record a heartbeat from an agent"""
        await self.store.save(heartbeat)
    
    async def get_agent_health(self, agent_id: str) -> HealthStatus:
        """Get current health status of an agent"""
        last_beat = await self.store.get_last(agent_id)
        if not last_beat:
            return HealthStatus.UNKNOWN
        
        elapsed = (datetime.now() - last_beat.timestamp).seconds
        if elapsed > last_beat.ttl * 2:
            return HealthStatus.CRITICAL
        elif elapsed > last_beat.ttl:
            return HealthStatus.WARNING
        return last_beat.status
    
    async def _check_all_agents(self):
        """Check all registered agents"""
        agents = await self.store.list_agents()
        for agent_id in agents:
            status = await self.get_agent_health(agent_id)
            if status in (HealthStatus.WARNING, HealthStatus.CRITICAL):
                await self._alert(agent_id, status)
```

### 3.4 Agent Heartbeat Client

```python
# sdk/heartbeat/client.py
class AgentHeartbeatClient:
    """Client for agents to send heartbeats"""
    
    def __init__(self, agent_id: str, monitor_url: str, interval: int = 30):
        self.agent_id = agent_id
        self.monitor_url = monitor_url
        self.interval = interval
        self._running = False
    
    async def start(self):
        """Start sending heartbeats"""
        self._running = True
        while self._running:
            await self._send_heartbeat()
            await asyncio.sleep(self.interval)
    
    async def _send_heartbeat(self):
        """Send heartbeat to monitor"""
        heartbeat = Heartbeat(
            agent_id=self.agent_id,
            timestamp=datetime.now(),
            status=await self._check_health(),
            metrics=await self._collect_metrics(),
            message=None,
            ttl=self.interval * 2
        )
        
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.monitor_url}/api/v1/heartbeat",
                json=heartbeat.to_dict()
            )
    
    async def _collect_metrics(self) -> dict:
        """Collect system metrics"""
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage('/').percent,
        }
```

### 3.5 Alert System

```python
# sdk/heartbeat/alerts.py
class AlertManager:
    """Manages health alerts"""
    
    def __init__(self):
        self.channels: list[AlertChannel] = []
        self.rules: list[AlertRule] = []
    
    def add_channel(self, channel: AlertChannel):
        """Add an alert channel (webhook, email, slack)"""
        self.channels.append(channel)
    
    async def trigger(self, alert: Alert):
        """Trigger an alert on all channels"""
        for channel in self.channels:
            await channel.send(alert)

class WebhookAlertChannel:
    async def send(self, alert: Alert):
        async with httpx.AsyncClient() as client:
            await client.post(self.webhook_url, json=alert.to_dict())
```

### 3.6 Heartbeat API Endpoints

```
POST /api/v1/heartbeat              # Receive heartbeat from agent
GET  /api/v1/heartbeat/{agent_id}   # Get agent health status
GET  /api/v1/heartbeat              # List all agent health statuses
WS   /ws/health                     # Real-time health updates
```

### 3.7 Tasks

- [ ] Create `sdk/heartbeat/` module
- [ ] Implement HeartbeatMonitor service
- [ ] Implement AgentHeartbeatClient
- [ ] Create health check framework
- [ ] Build AlertManager with webhook support
- [ ] Add heartbeat endpoints to API
- [ ] Integrate heartbeat into agent lifecycle
- [ ] Create health dashboard UI components
- [ ] Write tests for heartbeat system (15+ tests)

### Deliverables
- [ ] Heartbeat monitoring service
- [ ] Agent heartbeat clients
- [ ] Alert system with webhooks
- [ ] Health status API
- [ ] Test suite: 15+ passing tests

---

## 🎨 Phase 4: React Admin Portal

### 4.1 Tech Stack

- **Framework:** React 18 + TypeScript
- **Build Tool:** Vite
- **UI Library:** Tailwind CSS + Headless UI
- **State Management:** Zustand
- **Data Fetching:** TanStack Query (React Query)
- **Real-time:** WebSocket client
- **Charts:** Recharts
- **Icons:** Lucide React
- **Forms:** React Hook Form + Zod

### 4.2 Project Structure

```
web-ui/
├── index.html              # Entry HTML
├── package.json
├── vite.config.ts
├── tsconfig.json
├── tailwind.config.js
├── src/
│   ├── main.tsx            # App entry
│   ├── App.tsx             # Root component
│   ├── components/         # Shared components
│   │   ├── ui/             # Base UI (Button, Input, etc.)
│   │   ├── layout/         # Layout components
│   │   └── charts/         # Chart components
│   ├── features/           # Feature modules
│   │   ├── agents/         # Agent management
│   │   ├── memory/         # Memory browser
│   │   ├── workflows/      # Workflow editor
│   │   ├── skills/         # Skills management
│   │   ├── mcp/            # MCP configuration
│   │   ├── health/         # Health monitoring
│   │   └── admin/          # Admin settings
│   ├── hooks/              # Custom React hooks
│   ├── stores/             # Zustand stores
│   ├── api/                # API client
│   ├── types/              # TypeScript types
│   └── utils/              # Utilities
└── public/                 # Static assets
```

### 4.3 Core Features

#### Dashboard (Home)
- Real-time agent status overview
- System metrics charts
- Recent activity feed
- Quick actions (spawn agent, etc.)

#### Agents Management
- List view with filters/search
- Detail view with logs
- Spawn wizard with templates
- Bulk operations
- Real-time status updates

#### Memory Browser
- Namespace explorer
- Key-value editor
- Vector memory visualization
- Import/export tools

#### Workflow Editor
- Visual workflow builder
- Step configuration
- Execution history
- Templates gallery

#### Skills Management
- Skills registry view
- Skill configuration
- Capability explorer
- Built-in vs custom skills

#### MCP Configuration
- MCP server management
- Tool/resource explorer
- Connection testing
- Server logs

#### Health Monitoring
- Agent health status grid
- Heartbeat timeline
- Alert configuration
- System metrics dashboard

#### Admin Settings
- API key management
- Rate limit configuration
- Audit log viewer
- System configuration

### 4.4 API Client

```typescript
// src/api/client.ts
import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      refetchInterval: 10000,
    },
  },
});

export class ObscuraAPI {
  private baseUrl: string;
  private token: string;
  
  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl;
    this.token = token;
  }
  
  // Agents
  async listAgents(): Promise<Agent[]> { ... }
  async spawnAgent(config: AgentConfig): Promise<Agent> { ... }
  async stopAgent(id: string): Promise<void> { ... }
  
  // Memory
  async getMemory(namespace: string, key: string): Promise<any> { ... }
  async setMemory(namespace: string, key: string, value: any): Promise<void> { ... }
  
  // Skills
  async listSkills(): Promise<Skill[]> { ... }
  async executeSkill(name: string, params: any): Promise<any> { ... }
  
  // Health
  async getHealth(agentId: string): Promise<HealthStatus> { ... }
  
  // WebSocket
  connectWebSocket(): WebSocket { ... }
}
```

### 4.5 State Management

```typescript
// src/stores/agentStore.ts
import { create } from 'zustand';

interface AgentState {
  agents: Agent[];
  selectedAgent: Agent | null;
  setAgents: (agents: Agent[]) => void;
  selectAgent: (agent: Agent | null) => void;
  updateAgentStatus: (id: string, status: AgentStatus) => void;
}

export const useAgentStore = create<AgentState>((set) => ({
  agents: [],
  selectedAgent: null,
  setAgents: (agents) => set({ agents }),
  selectAgent: (agent) => set({ selectedAgent: agent }),
  updateAgentStatus: (id, status) => set((state) => ({
    agents: state.agents.map((a) =>
      a.id === id ? { ...a, status } : a
    ),
  })),
}));
```

### 4.6 WebSocket Integration

```typescript
// src/hooks/useWebSocket.ts
export function useWebSocket() {
  const [connected, setConnected] = useState(false);
  const updateAgentStatus = useAgentStore((s) => s.updateAgentStatus);
  
  useEffect(() => {
    const ws = new WebSocket(`${WS_URL}/ws/monitor`);
    
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'agent.update') {
        updateAgentStatus(data.agent_id, data.status);
      }
    };
    
    return () => ws.close();
  }, []);
  
  return { connected };
}
```

### 4.7 Tasks

- [ ] Initialize React project with Vite + TypeScript
- [ ] Set up Tailwind CSS and base UI components
- [ ] Create API client with React Query
- [ ] Build Dashboard layout and navigation
- [ ] Implement Agents management pages
- [ ] Implement Memory browser
- [ ] Implement Workflow editor
- [ ] Implement Skills management
- [ ] Implement MCP configuration
- [ ] Implement Health monitoring dashboard
- [ ] Implement Admin settings
- [ ] Add WebSocket real-time updates
- [ ] Add dark/light theme toggle
- [ ] Write component tests (30+ tests)

### Deliverables
- [ ] Complete React admin portal
- [ ] All 8 feature modules working
- [ ] Real-time WebSocket integration
- [ ] Responsive design (desktop + tablet)
- [ ] Test suite: 30+ passing tests

---

## 🔗 Phase 5: Integration & API Bridge

### 5.1 Backend-Frontend Integration

**CORS Configuration:**
```python
# sdk/server.py - Add CORS
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### 5.2 Unified API Response Format

```typescript
// Standard API response wrapper
interface ApiResponse<T> {
  data: T;
  meta?: {
    total?: number;
    page?: number;
    per_page?: number;
  };
  error?: {
    code: string;
    message: string;
    details?: any;
  };
}
```

### 5.3 Authentication Flow

```typescript
// src/auth/AuthProvider.tsx
export function AuthProvider({ children }) {
  const [token, setToken] = useState<string | null>(null);
  
  const login = async (apiKey: string) => {
    // Validate key with backend
    const valid = await api.validateKey(apiKey);
    if (valid) {
      setToken(apiKey);
      localStorage.setItem('obscura_token', apiKey);
    }
  };
  
  return (
    <AuthContext.Provider value={{ token, login }}>
      {children}
    </AuthContext.Provider>
  );
}
```

### 5.4 Build & Deployment

**Development:**
```bash
# Terminal 1 - Backend
cd ~/dev/obscura
export OBSCURA_AUTH_ENABLED=false
obscura serve --port 8080

# Terminal 2 - Frontend
cd ~/dev/obscura/web-ui
npm run dev  # Vite dev server on :5173
```

**Production Build:**
```bash
cd ~/dev/obscura/web-ui
npm run build  # Output to dist/

# Serve static files from backend
# Add to sdk/server.py:
# app.mount("/", StaticFiles(directory="web-ui/dist", html=True))
```

### 5.5 Tasks

- [ ] Configure CORS for frontend development
- [ ] Standardize API response formats
- [ ] Implement auth flow in frontend
- [ ] Create build scripts for production
- [ ] Add static file serving from backend
- [ ] Create docker-compose for full stack
- [ ] Write integration tests (15+ tests)

### Deliverables
- [ ] Backend-frontend integration complete
- [ ] Production build pipeline
- [ ] Docker deployment config
- [ ] Test suite: 15+ passing tests

---

## ✅ Phase 6: QA Phase

### 6.1 Test Strategy

| Category | Approach | Target |
|----------|----------|--------|
| Unit Tests | pytest (backend), Vitest (frontend) | >80% coverage |
| Integration Tests | API contract tests | All endpoints |
| E2E Tests | Playwright | Critical user flows |
| Load Tests | Locust | 100+ concurrent agents |
| Security Audit | OWASP checklist | No critical issues |

### 6.2 Backend Test Matrix

```
tests/
├── unit/
│   ├── test_mcp.py              # MCP integration (20 tests)
│   ├── test_skills.py           # Skills framework (25 tests)
│   ├── test_heartbeat.py        # Heartbeat system (15 tests)
│   └── test_integration.py      # API integration (15 tests)
├── e2e/
│   ├── test_full_stack.py       # End-to-end (10 tests)
│   └── test_websocket.py        # Real-time (5 tests)
├── load/
│   └── test_load.py             # Load testing
└── security/
    └── test_security.py         # Security tests
```

### 6.3 Frontend Test Matrix

```
web-ui/src/
├── components/
│   └── **/*.test.tsx            # Component tests (30 tests)
├── features/
│   └── **/*.test.tsx            # Feature tests (20 tests)
├── e2e/
│   └── *.spec.ts                # Playwright E2E (10 tests)
```

### 6.4 QA Checklist

#### Backend QA
- [ ] All unit tests passing (75+ tests)
- [ ] All integration tests passing (25+ tests)
- [ ] API documentation accurate
- [ ] MCP server responds to stdio/SSE
- [ ] Skills execute correctly
- [ ] Heartbeats detect failures
- [ ] WebSocket connections stable
- [ ] Memory operations atomic
- [ ] Auth middleware working
- [ ] Rate limiting enforced

#### Frontend QA
- [ ] All component tests passing (30+ tests)
- [ ] All E2E tests passing (10+ tests)
- [ ] Responsive on desktop/tablet
- [ ] Dark/light theme works
- [ ] Real-time updates working
- [ ] Forms validate correctly
- [ ] Error handling graceful
- [ ] Loading states visible
- [ ] No console errors
- [ ] Accessibility check (a11y)

#### Integration QA
- [ ] Frontend connects to backend
- [ ] Auth flow works end-to-end
- [ ] WebSocket reconnects on drop
- [ ] File uploads work
- [ ] Export/import works
- [ ] Docker compose works

### 6.5 Tasks

- [ ] Write backend unit tests (75+)
- [ ] Write frontend unit tests (30+)
- [ ] Write E2E tests with Playwright (10+)
- [ ] Run load tests (100+ agents)
- [ ] Perform security audit
- [ ] Fix all critical/high bugs
- [ ] Update documentation
- [ ] Create QA report

### Deliverables
- [ ] Test suite: 150+ passing tests
- [ ] QA report with metrics
- [ ] Security audit report
- [ ] Load test results

---

## 👥 Phase 7: UAT Phase

### 7.1 UAT Environment

```yaml
# docker-compose.uat.yml
version: '3.8'
services:
  obscura-backend:
    build: .
    ports:
      - "8080:8080"
    environment:
      - OBSCURA_ENV=uat
      - OBSCURA_AUTH_ENABLED=true
  
  obscura-frontend:
    build: ./web-ui
    ports:
      - "3000:80"
    depends_on:
      - obscura-backend
```

### 7.2 UAT Test Cases

| ID | Scenario | Steps | Expected Result |
|----|----------|-------|-----------------|
| UAT-1 | Deploy full stack | Run docker-compose | All services start |
| UAT-2 | Login to admin | Enter API key | Access granted |
| UAT-3 | Spawn agent via UI | Click "Spawn", fill form | Agent appears in list |
| UAT-4 | Agent heartbeat | Wait 30 seconds | Health shows "healthy" |
| UAT-5 | Use MCP tool | Connect MCP server, run tool | Tool executes successfully |
| UAT-6 | Execute skill | Select skill, run capability | Skill executes |
| UAT-7 | Create workflow | Build visual workflow | Workflow saved |
| UAT-8 | Run workflow | Execute workflow | Steps complete in order |
| UAT-9 | Monitor health | View health dashboard | All agents show status |
| UAT-10 | Kill agent | Stop agent process | Health shows "critical" |
| UAT-11 | Receive alert | Trigger health alert | Alert received via webhook |
| UAT-12 | Memory browser | View/edit memory | Changes persisted |
| UAT-13 | Export data | Click export | JSON file downloaded |
| UAT-14 | Responsive test | Resize browser | Layout adapts |
| UAT-15 | 100 agents | Spawn 100 agents | System remains stable |

### 7.3 Sign-off Criteria

- [ ] All UAT test cases pass
- [ ] No critical or high bugs open
- [ ] Performance meets targets (< 200ms API response)
- [ ] Documentation complete
- [ ] User guide written
- [ ] Deployment guide tested

### 7.4 Tasks

- [ ] Deploy UAT environment
- [ ] Execute UAT test cases
- [ ] Collect feedback
- [ ] Fix UAT bugs
- [ ] Get sign-off
- [ ] Tag v1.0 release

### Deliverables
- [ ] UAT test results
- [ ] Sign-off document
- [ ] v1.0 release tagged
- [ ] Production deployment guide

---

## 📊 Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Test Coverage | >80% | Coverage report |
| API Response Time | <200ms p95 | Load test |
| Concurrent Agents | 100+ | Load test |
| Uptime | 99.9% | Heartbeat monitoring |
| Frontend Bundle | <500KB | Build output |
| E2E Test Pass Rate | 100% | Playwright report |
| Security Issues | 0 critical | Audit report |

---

## 🚀 Getting Started

### Quick Start for Development

```bash
# 1. Clone and setup
cd ~/dev/obscura
pip install -e ".[dev,server,mcp,skills,heartbeat]"

# 2. Start backend
export OBSCURA_AUTH_ENABLED=false
obscura serve --port 8080

# 3. Start frontend (new terminal)
cd web-ui
npm install
npm run dev

# 4. Open browser
open http://localhost:5173
```

### Running Tests

```bash
# Backend tests
pytest tests/ -v --cov=sdk --cov-report=html

# Frontend tests
cd web-ui
npm test

# E2E tests
npm run test:e2e

# Load tests
locust -f tests/load/test_load.py
```

---

## 📝 Notes

### Open Questions

1. **MCP Server Deployment:** Should we bundle MCP servers or require external setup?
2. **Skill Marketplace:** MVP or future feature?
3. **Cloud vs Self-hosted:** Is there a cloud offering planned?
4. **Authentication:** Stick with API keys or add OAuth?
5. **Database:** Keep in-memory or add persistent storage option?

### Decisions Made

- React + TypeScript for admin portal
- FastMCP for MCP implementation
- Zustand for state management
- Playwright for E2E testing
- Docker Compose for deployment

---

*Plan created: 2026-02-08*
*Next step: Spawn agents for Phase 1-3 (Backend work)*

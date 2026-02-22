// ========== Agents ==========

export interface Agent {
  agent_id: string;
  name: string;
  status: 'running' | 'idle' | 'error' | 'stopped' | 'pending' | 'waiting' | 'completed' | 'failed';
  model?: string;
  created_at: string;
  updated_at?: string;
  iteration_count?: number;
  error_message?: string | null;
  mcp_enabled?: boolean;
  tags?: string[];
  id: string; // UI alias for agent_id
}

export interface CreateAgentRequest {
  name: string;
  backend: string;
  model?: string;
  system_prompt?: string;
  memory_namespace?: string;
  tools?: string[];
  skills?: string[];
  config?: Record<string, unknown>;
}

export interface AgentsResponse {
  agents: Omit<Agent, 'id'>[];
  count: number;
}

export interface AgentTemplate {
  template_id: string;
  name: string;
  backend: string;
  model?: string;
  system_prompt?: string;
  tools?: string[];
  config?: Record<string, unknown>;
}

export interface AgentGroup {
  group_id: string;
  name: string;
  agent_ids: string[];
  created_at: string;
}

// ========== Memory ==========

export interface MemoryEntry {
  namespace: string;
  key: string;
  value: unknown;
  created_at?: string;
  updated_at?: string;
}

export interface NamespaceStatsResponse {
  namespace: string;
  key_count: number;
  total_size_bytes: number;
}

export interface VectorMemoryResult {
  namespace: string;
  key: string;
  text: string;
  score: number;
  final_score: number;
  memory_type: string;
  metadata: Record<string, unknown>;
}

export interface VectorSearchResponse {
  query: string;
  results: VectorMemoryResult[];
  count: number;
}

// ========== Workflows ==========

export interface WorkflowStep {
  name: string;
  agent_config?: Record<string, unknown>;
  depends_on?: string[];
}

export interface Workflow {
  id: string;
  workflow_id: string;
  name: string;
  description: string;
  status: 'active' | 'paused' | 'archived';
  steps: WorkflowStep[];
  step_count: number;
  created_at?: string;
}

export interface WorkflowExecution {
  execution_id: string;
  workflow_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  started_at?: string;
  completed_at?: string;
  outputs?: Record<string, unknown>;
  step_results?: Record<string, unknown>;
}

// ========== Skills ==========

export interface SkillCapability {
  name: string;
  description: string;
  parameters: {
    name: string;
    type: string;
    description: string;
    required: boolean;
  }[];
}

export interface Skill {
  name: string;
  version: string;
  description: string;
  capabilities: SkillCapability[];
  metadata?: {
    author: string;
    category: string;
    tags: string[];
  };
}

// ========== Health ==========

export interface AgentHealth {
  agent_id: string;
  status: 'healthy' | 'warning' | 'critical' | 'unknown';
  last_heartbeat?: {
    timestamp: string;
    status: string;
  };
  expected_interval?: number;
  missed_count?: number;
  metrics?: {
    cpu_percent: number;
    memory_percent: number;
    disk_usage?: number;
  };
}

export interface HealthSummary {
  total: number;
  healthy: number;
  warning: number;
  critical: number;
  unknown: number;
  agents: {
    agent_id: string;
    status: string;
    last_heartbeat: string | null;
    missed_count: number;
  }[];
  auth_enabled?: boolean;
}

// ========== Metrics ==========

export interface SystemMetrics {
  agents: { total: number; running: number; idle: number; error: number };
  memory: { namespaces: number; total_keys: number };
  templates: { total: number };
  workflows: { total: number; active: number };
  webhooks: { total: number; active: number };
  timestamp: string;
}

// ========== Webhooks ==========

export interface Webhook {
  webhook_id: string;
  url: string;
  events: string[];
  active: boolean;
  created_at: string;
}

// ========== Tool Approvals ==========

export interface ToolApproval {
  approval_id: string;
  user_id: string;
  agent_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  status: 'pending' | 'approved' | 'denied' | 'expired';
  created_at: string;
  resolved_at?: string;
  reason?: string;
}

// ========== Audit ==========

export interface AuditLogEntry {
  id: string;
  timestamp: string;
  user_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  outcome: 'success' | 'failure';
  details?: Record<string, unknown>;
}

export interface AuditSummary {
  total_events: number;
  by_action: Record<string, number>;
  by_outcome: Record<string, number>;
  by_user: Record<string, number>;
}

// ========== Sessions ==========

export interface Session {
  session_id: string;
  backend: string;
  created_at: string;
}

// ========== Capabilities ==========

export interface CapabilityTier {
  tier: 'PUBLIC' | 'PRIVILEGED';
  roles: string[];
}

// ========== Rate Limits ==========

export interface RateLimit {
  api_key: string;
  requests_per_minute: number;
  requests_per_hour: number;
}

// ========== A2A ==========

export interface A2AAgentCard {
  name: string;
  description: string;
  url: string;
  capabilities: string[];
}

export interface A2ATask {
  task_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  messages: { role: string; content: string }[];
  created_at: string;
  updated_at?: string;
}

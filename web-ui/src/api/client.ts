import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8080';

// Generic fetch wrapper
async function fetchApi<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  const response = await fetch(`${API_URL}${endpoint}`, {
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
    ...options,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ message: 'Unknown error' }));
    throw new Error(error.detail || error.message || `HTTP ${response.status}`);
  }

  return response.json();
}

// ========== AGENTS API ==========

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
  // UI convenience alias
  id: string;
}

interface AgentsResponse {
  agents: Omit<Agent, 'id'>[];
  count: number;
}

function normalizeAgent(raw: Omit<Agent, 'id'>): Agent {
  return { ...raw, id: raw.agent_id };
}

export interface CreateAgentRequest {
  name: string;
  backend: string;
  model?: string;
  system_prompt?: string;
  memory_namespace?: string;
  tools?: string[];
  skills?: string[];
  config?: Record<string, any>;
}

export function useAgents() {
  return useQuery({
    queryKey: ['agents'],
    queryFn: async () => {
      const resp = await fetchApi<AgentsResponse>('/api/v1/agents');
      return resp.agents.map(normalizeAgent);
    },
    refetchInterval: 5000,
  });
}

export function useAgent(id: string) {
  return useQuery({
    queryKey: ['agent', id],
    queryFn: async () => {
      const raw = await fetchApi<Omit<Agent, 'id'>>(`/api/v1/agents/${id}`);
      return normalizeAgent(raw);
    },
    enabled: !!id,
  });
}

export function useSpawnAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: CreateAgentRequest) =>
      fetchApi<any>('/api/v1/agents', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

export function useStopAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<void>(`/api/v1/agents/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

export function useRunAgent() {
  return useMutation({
    mutationFn: ({ id, prompt }: { id: string; prompt: string }) =>
      fetchApi<any>(`/api/v1/agents/${id}/run`, {
        method: 'POST',
        body: JSON.stringify({ prompt }),
      }),
  });
}

// ========== MEMORY API ==========

export interface MemoryNamespace {
  namespace_id: string;
  description?: string;
  ttl_days?: number;
  created_by?: string;
  created_at?: string;
}

export interface MemoryEntry {
  namespace: string;
  key: string;
  value: any;
  created_at?: string;
  updated_at?: string;
}

interface NamespacesResponse {
  namespaces: string[];
  count: number;
}

interface MemoryKeysResponse {
  keys: { namespace: string; key: string }[];
  count: number;
}

interface NamespaceStatsResponse {
  namespace: string;
  key_count: number;
  total_size_bytes: number;
}

export function useMemoryNamespaces() {
  return useQuery({
    queryKey: ['memory', 'namespaces'],
    queryFn: async () => {
      const resp = await fetchApi<NamespacesResponse>('/api/v1/memory/namespaces');
      return resp.namespaces;
    },
  });
}

export function useMemoryKeys(namespace: string) {
  return useQuery({
    queryKey: ['memory', namespace, 'keys'],
    queryFn: async () => {
      const resp = await fetchApi<MemoryKeysResponse>(`/api/v1/memory`);
      // Filter keys for this namespace
      return resp.keys.filter(k => k.namespace === namespace);
    },
    enabled: !!namespace,
  });
}

export function useMemoryValue(namespace: string, key: string) {
  return useQuery({
    queryKey: ['memory', namespace, key],
    queryFn: () => fetchApi<{ namespace: string; key: string; value: any }>(
      `/api/v1/memory/${namespace}/${key}`
    ),
    enabled: !!namespace && !!key,
  });
}

export function useSetMemory() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, key, value }: { namespace: string; key: string; value: any }) =>
      fetchApi<void>(`/api/v1/memory/${namespace}/${key}`, {
        method: 'POST',
        body: JSON.stringify({ value }),
      }),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['memory', vars.namespace, vars.key] });
      queryClient.invalidateQueries({ queryKey: ['memory', vars.namespace, 'keys'] });
    },
  });
}

export function useDeleteMemory() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ namespace, key }: { namespace: string; key: string }) =>
      fetchApi<void>(`/api/v1/memory/${namespace}/${key}`, { method: 'DELETE' }),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['memory', vars.namespace] });
    },
  });
}

export function useMemoryNamespaceStats(namespace: string) {
  return useQuery({
    queryKey: ['memory', namespace, 'stats'],
    queryFn: () => fetchApi<NamespaceStatsResponse>(
      `/api/v1/memory/namespaces/${namespace}/stats`
    ),
    enabled: !!namespace,
  });
}

export function useExportMemory() {
  return useQuery({
    queryKey: ['memory', 'export'],
    queryFn: () => fetchApi<any>('/api/v1/memory/export'),
    enabled: false, // manual trigger only
  });
}

export function useImportMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: any) =>
      fetchApi<any>('/api/v1/memory/import', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory'] });
    },
  });
}

// ========== VECTOR MEMORY API ==========

export interface VectorMemoryResult {
  namespace: string;
  key: string;
  text: string;
  score: number;
  final_score: number;
  memory_type: string;
  metadata: Record<string, any>;
}

interface VectorSearchResponse {
  query: string;
  results: VectorMemoryResult[];
  count: number;
}

export function useVectorMemorySearch(
  query: string,
  options?: { namespace?: string; top_k?: number }
) {
  return useQuery({
    queryKey: ['vector-memory', 'search', query, options],
    queryFn: () => {
      const params = new URLSearchParams({ q: query, top_k: String(options?.top_k ?? 20) });
      if (options?.namespace) params.set('namespace', options.namespace);
      return fetchApi<VectorSearchResponse>(`/api/v1/vector-memory/search?${params}`);
    },
    enabled: query.length > 1,
  });
}

// ========== WORKFLOWS API ==========

export interface WorkflowStep {
  name: string;
  agent_config?: Record<string, any>;
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
  outputs?: Record<string, any>;
  step_results?: Record<string, any>;
}

interface WorkflowsResponse {
  workflows: Workflow[];
  count: number;
}

interface ExecutionsResponse {
  workflow_id: string;
  executions: WorkflowExecution[];
  count: number;
}

export function useWorkflows() {
  return useQuery({
    queryKey: ['workflows'],
    queryFn: async () => {
      const resp = await fetchApi<WorkflowsResponse>('/api/v1/workflows');
      return resp.workflows.map(w => ({ ...w, id: w.workflow_id || w.id }));
    },
  });
}

export function useWorkflow(id: string) {
  return useQuery({
    queryKey: ['workflow', id],
    queryFn: () => fetchApi<Workflow>(`/api/v1/workflows/${id}`),
    enabled: !!id,
  });
}

export function useCreateWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { name: string; description: string; steps: WorkflowStep[] }) =>
      fetchApi<Workflow>('/api/v1/workflows', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflows'] });
    },
  });
}

export function useDeleteWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<void>(`/api/v1/workflows/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflows'] });
    },
  });
}

export function useExecuteWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, inputs }: { id: string; inputs?: Record<string, any> }) =>
      fetchApi<WorkflowExecution>(`/api/v1/workflows/${id}/execute`, {
        method: 'POST',
        body: JSON.stringify({ inputs: inputs || {} }),
      }),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['workflow-executions', vars.id] });
    },
  });
}

export function useWorkflowExecutions(workflowId: string) {
  return useQuery({
    queryKey: ['workflow-executions', workflowId],
    queryFn: async () => {
      const resp = await fetchApi<ExecutionsResponse>(
        `/api/v1/workflows/${workflowId}/executions`
      );
      return resp.executions;
    },
    enabled: !!workflowId,
  });
}

// ========== SKILLS API ==========

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

export function useSkills() {
  return useQuery({
    queryKey: ['skills'],
    queryFn: () => fetchApi<Skill[]>('/api/v1/skills'),
  });
}

export function useSkill(name: string) {
  return useQuery({
    queryKey: ['skill', name],
    queryFn: () => fetchApi<Skill>(`/api/v1/skills/${name}`),
    enabled: !!name,
  });
}

export function useExecuteSkill() {
  return useMutation({
    mutationFn: ({ name, capability, params }: { name: string; capability: string; params: any }) =>
      fetchApi<any>(`/api/v1/skills/${name}/execute`, {
        method: 'POST',
        body: JSON.stringify({ capability, params }),
      }),
  });
}

// ========== HEALTH API ==========

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
  // Additional fields that may be present from the server
  auth_enabled?: boolean;
  [key: string]: unknown;
}

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => fetchApi<HealthSummary>('/api/v1/health'),
    refetchInterval: 10000,
  });
}

export function useAgentHealth(agentId: string) {
  return useQuery({
    queryKey: ['health', agentId],
    queryFn: () => fetchApi<AgentHealth>(`/api/v1/heartbeat/${agentId}`),
    enabled: !!agentId,
    refetchInterval: 5000,
  });
}

// ========== METRICS API ==========

export interface SystemMetrics {
  agents: {
    total: number;
    running: number;
    idle: number;
    error: number;
  };
  memory: {
    namespaces: number;
    total_keys: number;
  };
  templates: {
    total: number;
  };
  workflows: {
    total: number;
    active: number;
  };
  webhooks: {
    total: number;
    active: number;
  };
  timestamp: string;
}

export function useMetrics() {
  return useQuery({
    queryKey: ['metrics'],
    queryFn: () => fetchApi<SystemMetrics>('/api/v1/metrics'),
    refetchInterval: 10000,
  });
}

// ========== TEMPLATES API ==========

export interface AgentTemplate {
  template_id: string;
  name: string;
  backend: string;
  model?: string;
  system_prompt?: string;
  tools?: string[];
  config?: Record<string, any>;
}

interface TemplatesResponse {
  templates: AgentTemplate[];
  count: number;
}

export function useAgentTemplates() {
  return useQuery({
    queryKey: ['agent-templates'],
    queryFn: async () => {
      const resp = await fetchApi<TemplatesResponse>('/api/v1/agent-templates');
      return resp.templates;
    },
  });
}

// ========== WEBHOOKS API ==========

export interface Webhook {
  webhook_id: string;
  url: string;
  events: string[];
  active: boolean;
  created_at: string;
}

interface WebhooksResponse {
  webhooks: Webhook[];
  count: number;
}

export function useWebhooks() {
  return useQuery({
    queryKey: ['webhooks'],
    queryFn: async () => {
      const resp = await fetchApi<WebhooksResponse>('/api/v1/webhooks');
      return resp.webhooks;
    },
  });
}

// ========== AUDIT API ==========

export function useAuditLogs(limit = 50, offset = 0) {
  return useQuery({
    queryKey: ['audit-logs', limit, offset],
    queryFn: () => fetchApi<any>(`/api/v1/audit/logs?limit=${limit}&offset=${offset}`),
  });
}

export function useAuditSummary() {
  return useQuery({
    queryKey: ['audit-summary'],
    queryFn: () => fetchApi<any>('/api/v1/audit/logs/summary'),
  });
}

// ========== RATE LIMITS API ==========

export function useRateLimits() {
  return useQuery({
    queryKey: ['rate-limits'],
    queryFn: () => fetchApi<any>('/api/v1/rate-limits'),
  });
}

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type {
  Agent,
  AgentsResponse,
  CreateAgentRequest,
  AgentTemplate,
  AgentGroup,
} from '@/api/types';

// ========== Agents ==========

export function useAgents() {
  return useQuery({
    queryKey: ['agents'],
    queryFn: async () => {
      const data = await fetchApi<AgentsResponse>('/api/v1/agents');
      return {
        ...data,
        agents: data.agents.map((a) => ({ ...a, id: a.agent_id })),
      };
    },
    refetchInterval: 5000,
  });
}

export function useAgent(id: string | undefined) {
  return useQuery({
    queryKey: ['agents', id],
    queryFn: () => fetchApi<Agent>(`/api/v1/agents/${id}`),
    enabled: !!id,
  });
}

export function useAgentTools(id: string | undefined) {
  return useQuery({
    queryKey: ['agents', id, 'tools'],
    queryFn: () => fetchApi<string[]>(`/api/v1/agents/${id}/tools`),
    enabled: !!id,
  });
}

export function useAgentPeers(id: string | undefined) {
  return useQuery({
    queryKey: ['agents', id, 'peers'],
    queryFn: () => fetchApi<string[]>(`/api/v1/agents/${id}/peers`),
    enabled: !!id,
  });
}

export function useAgentTags(id: string | undefined) {
  return useQuery({
    queryKey: ['agents', id, 'tags'],
    queryFn: () => fetchApi<string[]>(`/api/v1/agents/${id}/tags`),
    enabled: !!id,
  });
}

export function useAgentMessages(id: string | undefined) {
  return useQuery({
    queryKey: ['agents', id, 'messages'],
    queryFn: () =>
      fetchApi<{ role: string; content: string }[]>(
        `/api/v1/agents/${id}/messages`
      ),
    enabled: !!id,
  });
}

export function useSpawnAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (req: CreateAgentRequest) =>
      fetchApi<Agent>('/api/v1/agents', {
        method: 'POST',
        body: JSON.stringify(req),
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
    mutationFn: ({
      id,
      input,
    }: {
      id: string;
      input?: Record<string, unknown>;
    }) =>
      fetchApi<{ result: unknown }>(`/api/v1/agents/${id}/run`, {
        method: 'POST',
        body: JSON.stringify(input ?? {}),
      }),
  });
}

export function useBulkSpawnAgents() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (requests: CreateAgentRequest[]) =>
      fetchApi<Agent[]>('/api/v1/agents/bulk', {
        method: 'POST',
        body: JSON.stringify(requests),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

export function useBulkStopAgents() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) =>
      fetchApi<void>('/api/v1/agents/bulk/stop', {
        method: 'POST',
        body: JSON.stringify({ agent_ids: ids }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

export function useBulkTagAgents() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      agent_ids,
      tags,
    }: {
      agent_ids: string[];
      tags: string[];
    }) =>
      fetchApi<void>('/api/v1/agents/bulk/tag', {
        method: 'POST',
        body: JSON.stringify({ agent_ids, tags }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

export function useAddAgentTags() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, tags }: { id: string; tags: string[] }) =>
      fetchApi<void>(`/api/v1/agents/${id}/tags`, {
        method: 'POST',
        body: JSON.stringify({ tags }),
      }),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: ['agents', id, 'tags'] });
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

export function useRemoveAgentTags() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, tags }: { id: string; tags: string[] }) =>
      fetchApi<void>(`/api/v1/agents/${id}/tags/remove`, {
        method: 'POST',
        body: JSON.stringify({ tags }),
      }),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: ['agents', id, 'tags'] });
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

export function useAgentSendMessage() {
  return useMutation({
    mutationFn: ({
      from,
      to,
      message,
    }: {
      from: string;
      to: string;
      message: string;
    }) =>
      fetchApi<void>(`/api/v1/agents/${from}/send/${to}`, {
        method: 'POST',
        body: JSON.stringify({ message }),
      }),
  });
}

export function useSpawnFromTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (req: { template_id: string; name?: string }) =>
      fetchApi<Agent>('/api/v1/agents/from-template', {
        method: 'POST',
        body: JSON.stringify(req),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agents'] });
    },
  });
}

// ========== Agent Templates ==========

export function useAgentTemplates() {
  return useQuery({
    queryKey: ['agent-templates'],
    queryFn: () => fetchApi<AgentTemplate[]>('/api/v1/agent-templates'),
  });
}

export function useAgentTemplate(id: string | undefined) {
  return useQuery({
    queryKey: ['agent-templates', id],
    queryFn: () => fetchApi<AgentTemplate>(`/api/v1/agent-templates/${id}`),
    enabled: !!id,
  });
}

export function useCreateTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (template: Omit<AgentTemplate, 'template_id'>) =>
      fetchApi<AgentTemplate>('/api/v1/agent-templates', {
        method: 'POST',
        body: JSON.stringify(template),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-templates'] });
    },
  });
}

export function useUpdateTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...data
    }: Partial<AgentTemplate> & { id: string }) =>
      fetchApi<AgentTemplate>(`/api/v1/agent-templates/${id}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: ['agent-templates'] });
      queryClient.invalidateQueries({ queryKey: ['agent-templates', id] });
    },
  });
}

export function useDeleteTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<void>(`/api/v1/agent-templates/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-templates'] });
    },
  });
}

// ========== Agent Groups ==========

export function useAgentGroups() {
  return useQuery({
    queryKey: ['agent-groups'],
    queryFn: () => fetchApi<AgentGroup[]>('/api/v1/agent-groups'),
  });
}

export function useAgentGroup(id: string | undefined) {
  return useQuery({
    queryKey: ['agent-groups', id],
    queryFn: () => fetchApi<AgentGroup>(`/api/v1/agent-groups/${id}`),
    enabled: !!id,
  });
}

export function useCreateAgentGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (group: { name: string; agent_ids: string[] }) =>
      fetchApi<AgentGroup>('/api/v1/agent-groups', {
        method: 'POST',
        body: JSON.stringify(group),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-groups'] });
    },
  });
}

export function useDeleteAgentGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<void>(`/api/v1/agent-groups/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-groups'] });
    },
  });
}

export function useBroadcastToGroup() {
  return useMutation({
    mutationFn: ({ id, message }: { id: string; message: string }) =>
      fetchApi<void>(`/api/v1/agent-groups/${id}/broadcast`, {
        method: 'POST',
        body: JSON.stringify({ message }),
      }),
  });
}

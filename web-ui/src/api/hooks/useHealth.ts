import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { HealthSummary, AgentHealth } from '@/api/types';

export function useHealthSummary() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => fetchApi<HealthSummary>('/api/v1/health'),
    refetchInterval: 10000,
  });
}

export function useAgentHealth(agentId: string | undefined) {
  return useQuery({
    queryKey: ['heartbeat', agentId],
    queryFn: () => fetchApi<AgentHealth>(`/api/v1/heartbeat/${agentId}`),
    enabled: !!agentId,
    refetchInterval: 5000,
  });
}

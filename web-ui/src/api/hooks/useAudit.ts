import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { AuditLogEntry, AuditSummary } from '@/api/types';

export function useAuditLogs(
  limit: number,
  offset: number,
  filters?: { action?: string; user_id?: string; resource_type?: string }
) {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (filters?.action) params.set('action', filters.action);
  if (filters?.user_id) params.set('user_id', filters.user_id);
  if (filters?.resource_type)
    params.set('resource_type', filters.resource_type);

  return useQuery({
    queryKey: ['audit', 'logs', limit, offset, filters],
    queryFn: () =>
      fetchApi<{ logs: AuditLogEntry[]; total: number }>(
        `/api/v1/audit/logs?${params.toString()}`
      ),
  });
}

export function useAuditSummary() {
  return useQuery({
    queryKey: ['audit', 'summary'],
    queryFn: () => fetchApi<AuditSummary>('/api/v1/audit/logs/summary'),
  });
}

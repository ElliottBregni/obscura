import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { Session } from '@/api/types';

export function useSessions() {
  return useQuery({
    queryKey: ['sessions'],
    queryFn: () => fetchApi<Session[]>('/api/v1/sessions'),
  });
}

export function useCreateSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (req: { backend: string }) =>
      fetchApi<Session>('/api/v1/sessions', {
        method: 'POST',
        body: JSON.stringify(req),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] });
    },
  });
}

export function useDeleteSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<void>(`/api/v1/sessions/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] });
    },
  });
}

export function useIngestSessions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (req?: {
      agent?: 'codex' | 'claude' | 'copilot';
      force?: boolean;
      copy_to_pwd?: boolean;
      copy_overwrite?: boolean;
    }) =>
      fetchApi<{ success: boolean; ingested: number; skipped: number; entries: number }>(
        '/api/v1/sessions/ingest',
        {
          method: 'POST',
          body: JSON.stringify(req ?? {}),
        }
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] });
      queryClient.invalidateQueries({ queryKey: ['vector-memory'] });
      queryClient.invalidateQueries({ queryKey: ['memory'] });
    },
  });
}

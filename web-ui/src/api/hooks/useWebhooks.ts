import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { Webhook } from '@/api/types';

export function useWebhooks() {
  return useQuery({
    queryKey: ['webhooks'],
    queryFn: async () => {
      const data = await fetchApi<{ webhooks: Webhook[]; count: number }>(
        '/api/v1/webhooks'
      );
      return data.webhooks;
    },
  });
}

export function useWebhook(id: string | undefined) {
  return useQuery({
    queryKey: ['webhooks', id],
    queryFn: () => fetchApi<Webhook>(`/api/v1/webhooks/${id}`),
    enabled: !!id,
  });
}

export function useCreateWebhook() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (webhook: { url: string; events: string[] }) =>
      fetchApi<Webhook>('/api/v1/webhooks', {
        method: 'POST',
        body: JSON.stringify(webhook),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['webhooks'] });
    },
  });
}

export function useDeleteWebhook() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<void>(`/api/v1/webhooks/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['webhooks'] });
    },
  });
}

export function useTestWebhook() {
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<{ success: boolean; status_code?: number }>(
        `/api/v1/webhooks/${id}/test`,
        { method: 'POST' }
      ),
  });
}

import { useMutation } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';

export function useTriggerSync() {
  return useMutation({
    mutationFn: () =>
      fetchApi<{ status: string }>('/api/v1/sync', { method: 'POST' }),
  });
}

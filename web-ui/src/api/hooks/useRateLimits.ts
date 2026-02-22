import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { RateLimit } from '@/api/types';

export function useRateLimits() {
  return useQuery({
    queryKey: ['rate-limits'],
    queryFn: () => fetchApi<RateLimit[]>('/api/v1/rate-limits'),
  });
}

export function useSetRateLimit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (limit: RateLimit) =>
      fetchApi<RateLimit>('/api/v1/rate-limits', {
        method: 'POST',
        body: JSON.stringify(limit),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rate-limits'] });
    },
  });
}

export function useDeleteRateLimit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (apiKey: string) =>
      fetchApi<void>(
        `/api/v1/rate-limits/${encodeURIComponent(apiKey)}`,
        { method: 'DELETE' }
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rate-limits'] });
    },
  });
}

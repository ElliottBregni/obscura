import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { SystemMetrics } from '@/api/types';

export function useSystemMetrics() {
  return useQuery({
    queryKey: ['metrics'],
    queryFn: () => fetchApi<SystemMetrics>('/api/v1/metrics'),
    refetchInterval: 10000,
  });
}

import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';

export function useObserveSnapshot() {
  return useQuery({
    queryKey: ['observe'],
    queryFn: () => fetchApi<Record<string, unknown>>('/api/v1/observe'),
  });
}

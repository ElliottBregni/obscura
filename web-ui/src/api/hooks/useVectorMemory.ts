import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { VectorSearchResponse } from '@/api/types';

export function useVectorSearch(
  query: string,
  opts?: { top_k?: number; namespace?: string }
) {
  const params = new URLSearchParams({ q: query });
  if (opts?.top_k) params.set('top_k', String(opts.top_k));
  if (opts?.namespace) params.set('namespace', opts.namespace);

  return useQuery({
    queryKey: ['vector-memory', 'search', query, opts],
    queryFn: () =>
      fetchApi<VectorSearchResponse>(
        `/api/v1/vector-memory/search?${params.toString()}`
      ),
    enabled: query.length > 0,
  });
}

export function useSetVectorMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      namespace,
      key,
      text,
      metadata,
    }: {
      namespace: string;
      key: string;
      text: string;
      metadata?: Record<string, unknown>;
    }) =>
      fetchApi<void>(
        `/api/v1/vector-memory/${encodeURIComponent(namespace)}/${encodeURIComponent(key)}`,
        {
          method: 'POST',
          body: JSON.stringify({ text, metadata }),
        }
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vector-memory'] });
    },
  });
}

export function useDeleteVectorMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ namespace, key }: { namespace: string; key: string }) =>
      fetchApi<void>(
        `/api/v1/vector-memory/${encodeURIComponent(namespace)}/${encodeURIComponent(key)}`,
        { method: 'DELETE' }
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vector-memory'] });
    },
  });
}

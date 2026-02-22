import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { MemoryEntry, NamespaceStatsResponse } from '@/api/types';

export function useMemoryNamespaces() {
  return useQuery({
    queryKey: ['memory', 'namespaces'],
    queryFn: async () => {
      const data = await fetchApi<{ namespaces: string[]; count: number }>(
        '/api/v1/memory/namespaces'
      );
      return data.namespaces;
    },
  });
}

export function useMemoryKeys(namespace: string | undefined) {
  return useQuery({
    queryKey: ['memory', 'keys', namespace],
    queryFn: async () => {
      const data = await fetchApi<{ keys: { namespace: string; key: string }[]; count: number }>(
        `/api/v1/memory?namespace=${encodeURIComponent(namespace!)}`
      );
      return data.keys;
    },
    enabled: !!namespace,
  });
}

export function useMemoryValue(
  namespace: string | undefined,
  key: string | undefined
) {
  return useQuery({
    queryKey: ['memory', namespace, key],
    queryFn: () =>
      fetchApi<MemoryEntry>(
        `/api/v1/memory/${encodeURIComponent(namespace!)}/${encodeURIComponent(key!)}`
      ),
    enabled: !!namespace && !!key,
  });
}

export function useMemorySearch(query: string) {
  return useQuery({
    queryKey: ['memory', 'search', query],
    queryFn: () =>
      fetchApi<MemoryEntry[]>(
        `/api/v1/memory/search?q=${encodeURIComponent(query)}`
      ),
    enabled: query.length > 0,
  });
}

export function useMemoryStats() {
  return useQuery({
    queryKey: ['memory', 'stats'],
    queryFn: () =>
      fetchApi<{ namespaces: number; total_keys: number }>(
        '/api/v1/memory/stats'
      ),
  });
}

export function useMemoryNamespaceStats(namespace: string | undefined) {
  return useQuery({
    queryKey: ['memory', 'namespaces', namespace, 'stats'],
    queryFn: () =>
      fetchApi<NamespaceStatsResponse>(
        `/api/v1/memory/namespaces/${encodeURIComponent(namespace!)}/stats`
      ),
    enabled: !!namespace,
  });
}

export function useSetMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      namespace,
      key,
      value,
    }: {
      namespace: string;
      key: string;
      value: unknown;
    }) =>
      fetchApi<void>(
        `/api/v1/memory/${encodeURIComponent(namespace)}/${encodeURIComponent(key)}`,
        {
          method: 'POST',
          body: JSON.stringify({ value }),
        }
      ),
    onSuccess: (_data, { namespace }) => {
      queryClient.invalidateQueries({ queryKey: ['memory', 'keys', namespace] });
      queryClient.invalidateQueries({ queryKey: ['memory', namespace] });
      queryClient.invalidateQueries({ queryKey: ['memory', 'stats'] });
    },
  });
}

export function useDeleteMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ namespace, key }: { namespace: string; key: string }) =>
      fetchApi<void>(
        `/api/v1/memory/${encodeURIComponent(namespace)}/${encodeURIComponent(key)}`,
        { method: 'DELETE' }
      ),
    onSuccess: (_data, { namespace }) => {
      queryClient.invalidateQueries({ queryKey: ['memory', 'keys', namespace] });
      queryClient.invalidateQueries({ queryKey: ['memory', namespace] });
      queryClient.invalidateQueries({ queryKey: ['memory', 'stats'] });
    },
  });
}

export function useCreateNamespace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (namespace: string) =>
      fetchApi<void>('/api/v1/memory/namespaces', {
        method: 'POST',
        body: JSON.stringify({ name: namespace }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory', 'namespaces'] });
      queryClient.invalidateQueries({ queryKey: ['memory', 'stats'] });
    },
  });
}

export function useDeleteNamespace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (namespace: string) =>
      fetchApi<void>(
        `/api/v1/memory/namespaces/${encodeURIComponent(namespace)}`,
        { method: 'DELETE' }
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory', 'namespaces'] });
      queryClient.invalidateQueries({ queryKey: ['memory', 'stats'] });
    },
  });
}

export function useMemoryTransaction() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (
      operations: {
        op: 'set' | 'delete';
        namespace: string;
        key: string;
        value?: unknown;
      }[]
    ) =>
      fetchApi<void>('/api/v1/memory/transaction', {
        method: 'POST',
        body: JSON.stringify({ operations }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory'] });
    },
  });
}

export function useExportMemory() {
  return useQuery({
    queryKey: ['memory', 'export'],
    queryFn: () =>
      fetchApi<Record<string, unknown>>('/api/v1/memory/export'),
    enabled: false,
  });
}

export function useImportMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: Record<string, unknown>) =>
      fetchApi<void>('/api/v1/memory/import', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory'] });
    },
  });
}

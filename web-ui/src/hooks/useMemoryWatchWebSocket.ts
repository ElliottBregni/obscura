import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useWebSocket } from './useWebSocket';

interface MemoryEvent {
  type: 'set' | 'delete' | string;
  namespace: string;
  key: string;
  value?: unknown;
}

export function useMemoryWatchWebSocket(namespace: string, enabled = true) {
  const queryClient = useQueryClient();

  const onMessage = useCallback(
    (data: unknown) => {
      const event = data as MemoryEvent;
      // Invalidate relevant queries when memory changes
      queryClient.invalidateQueries({ queryKey: ['memory', event.namespace, event.key] });
      queryClient.invalidateQueries({ queryKey: ['memory', event.namespace, 'keys'] });
    },
    [queryClient]
  );

  return useWebSocket({
    path: `/ws/memory/${encodeURIComponent(namespace)}`,
    onMessage,
    enabled: enabled && !!namespace,
  });
}

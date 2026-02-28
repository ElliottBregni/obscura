import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useWebSocket } from './useWebSocket';

type BroadcastEvent = {
  type: string;
  data?: {
    session_id?: string;
    backend?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export function useSessionBroadcastSync(enabled = true) {
  const queryClient = useQueryClient();

  const onMessage = useCallback(
    (payload: unknown) => {
      const event = payload as BroadcastEvent;
      if (!event?.type.startsWith('session_')) return;

      queryClient.invalidateQueries({ queryKey: ['sessions'] });
    },
    [queryClient],
  );

  return useWebSocket({
    path: '/ws/broadcast',
    onMessage,
    enabled,
  });
}

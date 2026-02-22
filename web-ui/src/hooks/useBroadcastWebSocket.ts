import { useCallback } from 'react';
import { useWebSocket } from './useWebSocket';
import { useAgentStore } from '@/stores/agentStore';

interface BroadcastEvent {
  type: 'agent_spawned' | 'agent_stopped' | 'agent_status' | string;
  agent_id?: string;
  name?: string;
  status?: string;
  [key: string]: unknown;
}

export function useBroadcastWebSocket(enabled = true) {
  const updateAgent = useAgentStore((s) => s.updateAgent);
  const removeAgent = useAgentStore((s) => s.removeAgent);

  const onMessage = useCallback(
    (data: unknown) => {
      const event = data as BroadcastEvent;
      switch (event.type) {
        case 'agent_spawned':
        case 'agent_status':
          if (event.agent_id) {
            updateAgent({
              agent_id: event.agent_id,
              name: event.name || event.agent_id,
              status: event.status || 'running',
            });
          }
          break;
        case 'agent_stopped':
          if (event.agent_id) {
            removeAgent(event.agent_id);
          }
          break;
      }
    },
    [updateAgent, removeAgent]
  );

  return useWebSocket({
    path: '/ws/broadcast',
    onMessage,
    enabled,
  });
}

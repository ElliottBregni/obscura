import { useCallback } from 'react';
import { useWebSocket } from './useWebSocket';
import { useAgentStore } from '@/stores/agentStore';
import { useSystemStore } from '@/stores/systemStore';

interface HealthEvent {
  type: string;
  agent_id?: string;
  status?: string;
  agents?: { agent_id: string; status: string }[];
}

export function useHealthWebSocket(enabled = true) {
  const updateAgent = useAgentStore((s) => s.updateAgent);
  const setWsConnected = useSystemStore((s) => s.setWsConnected);

  const onMessage = useCallback(
    (data: unknown) => {
      const event = data as HealthEvent;
      if (event.type === 'health_update' && event.agents) {
        for (const agent of event.agents) {
          updateAgent({
            agent_id: agent.agent_id,
            name: agent.agent_id,
            status: agent.status,
          });
        }
      }
    },
    [updateAgent]
  );

  const onOpen = useCallback(() => setWsConnected(true), [setWsConnected]);
  const onClose = useCallback(() => setWsConnected(false), [setWsConnected]);

  return useWebSocket({
    path: '/ws/health',
    onMessage,
    onOpen,
    onClose,
    enabled,
  });
}

import { useCallback } from 'react';
import { useWebSocket } from './useWebSocket';
import { useAgentStore } from '@/stores/agentStore';

interface AgentWsEvent {
  type: string;
  status?: string;
  data?: unknown;
}

export function useAgentWebSocket(agentId: string, enabled = true) {
  const updateAgent = useAgentStore((s) => s.updateAgent);

  const onMessage = useCallback(
    (data: unknown) => {
      const event = data as AgentWsEvent;
      if (event.status) {
        updateAgent({
          agent_id: agentId,
          name: agentId,
          status: event.status,
        });
      }
    },
    [agentId, updateAgent]
  );

  const ws = useWebSocket({
    path: `/ws/agents/${encodeURIComponent(agentId)}`,
    onMessage,
    enabled: enabled && !!agentId,
  });

  return ws;
}

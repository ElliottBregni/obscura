import { useEffect, useRef, useState, useCallback } from 'react';
import { useAgentStore } from '@/stores/agentStore';
import { useSystemStore } from '@/stores/systemStore';

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8080/ws/monitor';
const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_DELAY = 30000;

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelay = useRef(RECONNECT_DELAY);
  const [connected, setConnected] = useState(false);
  const { setAgents, updateAgentStatus, addAgent, removeAgent } = useAgentStore();
  const { setConnected: setSystemConnected } = useSystemStore();

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    try {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        setSystemConnected(true);
        reconnectDelay.current = RECONNECT_DELAY;
      };

      ws.onclose = () => {
        setConnected(false);
        setSystemConnected(false);
        // Auto-reconnect with exponential backoff
        reconnectTimeout.current = setTimeout(() => {
          reconnectDelay.current = Math.min(
            reconnectDelay.current * 1.5,
            MAX_RECONNECT_DELAY
          );
          connect();
        }, reconnectDelay.current);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);

          switch (data.type) {
            case 'init':
              if (data.agents) setAgents(data.agents);
              break;
            case 'agent.update':
            case 'agent_status':
              updateAgentStatus(data.agent_id, data.status);
              break;
            case 'agent.add':
            case 'agent_spawned':
              addAgent(data.agent);
              break;
            case 'agent.remove':
            case 'agent_stopped':
              removeAgent(data.agent_id);
              break;
          }
        } catch {
          // silently ignore parse errors
        }
      };

      ws.onerror = () => {
        // onclose will handle reconnection
      };
    } catch {
      // Connection failed, will retry via onclose
    }
  }, [setAgents, updateAgentStatus, addAgent, removeAgent, setSystemConnected]);

  useEffect(() => {
    connect();

    return () => {
      if (reconnectTimeout.current) clearTimeout(reconnectTimeout.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { connected, ws: wsRef.current };
}

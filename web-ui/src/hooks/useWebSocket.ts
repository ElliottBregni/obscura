import { useEffect, useRef, useCallback } from 'react';
import { WS_URL } from '@/lib/constants';
import { useAuthStore } from '@/stores/authStore';

interface UseWebSocketOptions {
  path: string;
  onMessage?: (data: unknown) => void;
  onOpen?: () => void;
  onClose?: () => void;
  enabled?: boolean;
  reconnectInterval?: number;
  maxReconnectAttempts?: number;
}

export function useWebSocket({
  path,
  onMessage,
  onOpen,
  onClose,
  enabled = true,
  reconnectInterval = 3000,
  maxReconnectAttempts = 10,
}: UseWebSocketOptions) {
  const wsRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout>>();

  const connect = useCallback(() => {
    if (!enabled) return;

    const { token, apiKey } = useAuthStore.getState();
    let url = `${WS_URL}${path}`;

    const params = new URLSearchParams();
    if (token) params.set('token', token);
    else if (apiKey) params.set('api_key', apiKey);
    if (params.toString()) url += `?${params}`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      attemptsRef.current = 0;
      onOpen?.();
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage?.(data);
      } catch {
        onMessage?.(event.data);
      }
    };

    ws.onclose = () => {
      onClose?.();
      if (enabled && attemptsRef.current < maxReconnectAttempts) {
        const delay = reconnectInterval * Math.pow(1.5, attemptsRef.current);
        attemptsRef.current++;
        reconnectTimerRef.current = setTimeout(connect, delay);
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [path, enabled, onMessage, onOpen, onClose, reconnectInterval, maxReconnectAttempts]);

  useEffect(() => {
    connect();

    return () => {
      clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  return { send };
}

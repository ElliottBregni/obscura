import { useEffect, useRef, useCallback, useState } from 'react';
import { API_URL } from '@/lib/constants';
import { useAuthStore } from '@/stores/authStore';

interface ObserveEvent {
  type: 'snapshot' | 'agent_state' | 'stalled' | 'agent_removed' | 'permission_required' | string;
  data?: unknown;
  agent_id?: string;
  [key: string]: unknown;
}

interface UseObserveStreamOptions {
  enabled?: boolean;
  intervalSeconds?: number;
  onEvent?: (event: ObserveEvent) => void;
}

export function useObserveStream({
  enabled = false,
  intervalSeconds = 2,
  onEvent,
}: UseObserveStreamOptions = {}) {
  const abortRef = useRef<AbortController | null>(null);
  const [connected, setConnected] = useState(false);

  const connect = useCallback(async () => {
    if (!enabled) return;

    const controller = new AbortController();
    abortRef.current = controller;

    const { token, apiKey } = useAuthStore.getState();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    else if (apiKey) headers['X-API-Key'] = apiKey;

    try {
      const response = await fetch(
        `${API_URL}/api/v1/observe/stream?interval_seconds=${intervalSeconds}&stale_seconds=20`,
        { headers, signal: controller.signal }
      );

      if (!response.ok || !response.body) {
        setConnected(false);
        return;
      }

      setConnected(true);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data:')) {
            try {
              const event = JSON.parse(line.slice(5).trim()) as ObserveEvent;
              onEvent?.(event);
            } catch {
              // skip malformed events
            }
          }
        }
      }
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        setConnected(false);
      }
    }
  }, [enabled, intervalSeconds, onEvent]);

  useEffect(() => {
    connect();
    return () => {
      abortRef.current?.abort();
    };
  }, [connect]);

  const disconnect = useCallback(() => {
    abortRef.current?.abort();
    setConnected(false);
  }, []);

  return { connected, disconnect };
}

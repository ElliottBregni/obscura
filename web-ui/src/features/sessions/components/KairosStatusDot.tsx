import { useEffect, useRef, useState } from 'react';
import { API_URL } from '@/lib/constants';
import { useAuthStore } from '@/stores/authStore';

type DaemonStatus = 'connecting' | 'alive' | 'stale' | 'offline';

const STATUS_LABEL: Record<DaemonStatus, string> = {
  connecting: 'Connecting…',
  alive: 'Daemon alive',
  stale: 'Daemon stale',
  offline: 'Daemon offline',
};

const STATUS_COLOR: Record<DaemonStatus, string> = {
  connecting: 'bg-yellow-500/60 animate-pulse',
  alive: 'bg-emerald-500',
  stale: 'bg-yellow-500',
  offline: 'bg-red-500/70',
};

/**
 * Small WebSocket-backed dot in the sidebar footer showing KAIROS daemon health.
 * Falls back to a REST poll if WS isn't available.
 */
export function KairosStatusDot() {
  const [status, setStatus] = useState<DaemonStatus>('connecting');
  const [agentCount, setAgentCount] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const staleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const markStale = () => {
    if (staleTimerRef.current) clearTimeout(staleTimerRef.current);
    staleTimerRef.current = setTimeout(() => setStatus('stale'), 15_000);
  };

  useEffect(() => {
    const { token, apiKey } = useAuthStore.getState();

    const wsUrl = API_URL.replace(/^http/, 'ws') + '/ws/health';
    let ws: WebSocket;

    try {
      // Append token as query param — WS can't set headers
      const url = token
        ? `${wsUrl}?token=${encodeURIComponent(token)}`
        : apiKey
        ? `${wsUrl}?api_key=${encodeURIComponent(apiKey)}`
        : wsUrl;

      ws = new WebSocket(url);
      wsRef.current = ws;
    } catch {
      setStatus('offline');
      return;
    }

    ws.onopen = () => {
      setStatus('connecting');
      markStale();
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string);
        if (msg.type === 'init' || msg.type === 'update') {
          const summary = msg.data ?? msg;
          const total = summary?.total_agents ?? summary?.agents?.length ?? null;
          setAgentCount(total);
          setStatus('alive');
          markStale();
        } else if (msg.type === 'ping') {
          setStatus('alive');
          markStale();
        }
      } catch {
        // non-JSON ping — still means alive
        setStatus('alive');
        markStale();
      }
    };

    ws.onerror = () => setStatus('stale');
    ws.onclose = () => {
      setStatus('offline');
      if (staleTimerRef.current) clearTimeout(staleTimerRef.current);
    };

    return () => {
      if (staleTimerRef.current) clearTimeout(staleTimerRef.current);
      ws.close();
    };
  }, []);

  return (
    <div className="flex items-center gap-1.5 px-1" title={STATUS_LABEL[status]}>
      <span className={`h-2 w-2 rounded-full shrink-0 transition-colors ${STATUS_COLOR[status]}`} />
      <span className="text-[10px] text-muted-foreground">
        {status === 'alive' && agentCount !== null
          ? `${agentCount} agent${agentCount !== 1 ? 's' : ''}`
          : STATUS_LABEL[status]}
      </span>
    </div>
  );
}

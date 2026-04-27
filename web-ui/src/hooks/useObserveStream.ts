import { useEffect, useRef, useState } from 'react';
import { useAuthStore } from '@/stores/authStore';

export interface ObservedAgent {
  agent_id: string;
  name: string;
  status: string;
  updated_at: string;
  iteration_count: number;
  error_message: string | null;
}

export interface ObserveSnapshot {
  timestamp: string;
  count: number;
  states: ObservedAgent[];
  stale_agent_ids: string[];
  pending_tool_approvals: { approval_id: string; tool_name: string }[];
}

export interface ObserveEvent {
  type: 'stalled' | 'state' | 'removed' | 'approval';
  agent_id: string;
  message: string;
  ts: number;
}

interface UseObserveStreamResult {
  snapshot: ObserveSnapshot | null;
  staleCount: number;
  connected: boolean;
  events: ObserveEvent[];
}

const MAX_EVENTS = 50;

function buildUrl(): string {
  const { token, apiKey } = useAuthStore.getState();
  const auth = token
    ? `&token=${encodeURIComponent(token)}`
    : apiKey
    ? `&api_key=${encodeURIComponent(apiKey)}`
    : '';
  return `/api/v1/observe/stream?interval_seconds=2${auth}`;
}

export function useObserveStream(active = true): UseObserveStreamResult {
  const [snapshot, setSnapshot] = useState<ObserveSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<ObserveEvent[]>([]);
  const esRef = useRef<EventSource | null>(null);

  const pushEvent = (ev: ObserveEvent) =>
    setEvents((prev) => [ev, ...prev].slice(0, MAX_EVENTS));

  useEffect(() => {
    if (!active) return;

    const es = new EventSource(buildUrl());
    esRef.current = es;

    es.addEventListener('open', () => setConnected(true));
    es.addEventListener('error', () => setConnected(false));

    es.addEventListener('snapshot', (e: MessageEvent) => {
      try { setSnapshot(JSON.parse(e.data) as ObserveSnapshot); } catch { /* */ }
    });

    es.addEventListener('stalled', (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as { agent_id: string; age_seconds: number };
        pushEvent({ type: 'stalled', agent_id: d.agent_id, message: `Stalled ${Math.round(d.age_seconds)}s`, ts: Date.now() });
      } catch { /* */ }
    });

    es.addEventListener('agent_state', (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as ObservedAgent;
        pushEvent({ type: 'state', agent_id: d.agent_id, message: `→ ${d.status}`, ts: Date.now() });
      } catch { /* */ }
    });

    es.addEventListener('agent_removed', (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as { agent_id: string };
        pushEvent({ type: 'removed', agent_id: d.agent_id, message: 'removed', ts: Date.now() });
      } catch { /* */ }
    });

    es.addEventListener('permission_required', (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as { tool_name: string };
        pushEvent({ type: 'approval', agent_id: '', message: `⚠ approval: ${d.tool_name}`, ts: Date.now() });
      } catch { /* */ }
    });

    return () => {
      es.close();
      esRef.current = null;
      setConnected(false);
    };
  }, [active]);

  return { snapshot, staleCount: snapshot?.stale_agent_ids.length ?? 0, connected, events };
}

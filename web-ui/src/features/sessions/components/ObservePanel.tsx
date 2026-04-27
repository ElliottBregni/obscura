import { useState } from 'react';
import { Radio, AlertTriangle, Circle, XCircle } from 'lucide-react';
import { useObserveStream } from '@/hooks/useObserveStream';
import type { ObservedAgent, ObserveEvent } from '@/hooks/useObserveStream';

const STATUS_COLOR: Record<string, string> = {
  RUNNING:   'text-emerald-500',
  WAITING:   'text-amber-400',
  IDLE:      'text-muted-foreground',
  STOPPED:   'text-muted-foreground/40',
  ERROR:     'text-red-500',
  FAILED:    'text-red-500',
};

const STATUS_DOT: Record<string, string> = {
  RUNNING:   'bg-emerald-500 animate-pulse',
  WAITING:   'bg-amber-400',
  IDLE:      'bg-muted-foreground/40',
  STOPPED:   'bg-muted-foreground/20',
  ERROR:     'bg-red-500',
  FAILED:    'bg-red-500',
};

const EVENT_ICON = {
  stalled:  <AlertTriangle className="h-2.5 w-2.5 text-amber-400 shrink-0" />,
  state:    <Circle className="h-2.5 w-2.5 text-muted-foreground shrink-0" />,
  removed:  <XCircle className="h-2.5 w-2.5 text-muted-foreground/50 shrink-0" />,
  approval: <AlertTriangle className="h-2.5 w-2.5 text-red-400 shrink-0" />,
} as const;

function AgentRow({ agent, isStale }: { agent: ObservedAgent; isStale: boolean }) {
  const dot = STATUS_DOT[agent.status] ?? 'bg-muted-foreground/40';
  const textColor = STATUS_COLOR[agent.status] ?? 'text-muted-foreground';
  const shortId = agent.agent_id.length > 12 ? agent.agent_id.slice(0, 10) + '…' : agent.agent_id;

  return (
    <div className={`flex items-start gap-1.5 rounded px-1.5 py-1 ${isStale ? 'bg-amber-500/10' : 'hover:bg-muted/40'} transition-colors`}>
      <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1">
          <span className="truncate text-[11px] font-medium text-foreground" title={agent.agent_id}>
            {agent.name || shortId}
          </span>
          {isStale && <AlertTriangle className="h-2.5 w-2.5 text-amber-400 shrink-0" />}
        </div>
        <div className="flex items-center gap-1.5 mt-0.5">
          <span className={`text-[10px] font-mono ${textColor}`}>{agent.status}</span>
          {agent.iteration_count > 0 && (
            <span className="text-[10px] text-muted-foreground/60">· {agent.iteration_count} iters</span>
          )}
        </div>
        {agent.error_message && (
          <p className="mt-0.5 truncate text-[10px] text-red-400" title={agent.error_message}>
            {agent.error_message}
          </p>
        )}
      </div>
    </div>
  );
}

function EventRow({ ev }: { ev: ObserveEvent }) {
  const shortId = ev.agent_id
    ? ev.agent_id.length > 12 ? ev.agent_id.slice(0, 10) + '…' : ev.agent_id
    : '';
  return (
    <div className="flex items-start gap-1.5 py-0.5">
      {EVENT_ICON[ev.type]}
      <span className="text-[10px] text-muted-foreground leading-tight">
        {shortId && <span className="font-mono text-foreground/70">{shortId} </span>}
        {ev.message}
      </span>
    </div>
  );
}

type SubTab = 'agents' | 'events';

export function ObservePanel() {
  const { snapshot, staleCount, connected, events } = useObserveStream(true);
  const [subTab, setSubTab] = useState<SubTab>('agents');

  const staleSet = new Set(snapshot?.stale_agent_ids ?? []);
  const agents = snapshot?.states ?? [];

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-1.5 px-2 pt-2 pb-1">
        <Radio className="h-3 w-3 text-muted-foreground" />
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground flex-1">
          Runtime
        </span>
        <span className={`h-1.5 w-1.5 rounded-full ${connected ? 'bg-emerald-500 animate-pulse' : 'bg-muted-foreground/30'}`} />
        {agents.length > 0 && (
          <span className="text-[10px] text-muted-foreground">{agents.length}</span>
        )}
        {staleCount > 0 && (
          <span className="rounded bg-amber-500/20 px-1 text-[10px] font-semibold text-amber-400">
            {staleCount} stale
          </span>
        )}
      </div>

      {/* Sub-tabs */}
      <div className="flex border-b border-border mx-2">
        <button
          onClick={() => setSubTab('agents')}
          className={`flex-1 py-1 text-[10px] font-semibold uppercase tracking-wider transition-colors
            ${subTab === 'agents' ? 'border-b border-primary text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
        >
          Agents
        </button>
        <button
          onClick={() => setSubTab('events')}
          className={`flex-1 py-1 text-[10px] font-semibold uppercase tracking-wider transition-colors relative
            ${subTab === 'events' ? 'border-b border-primary text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
        >
          Events
          {events.length > 0 && (
            <span className="ml-1 rounded-full bg-muted px-1 text-[9px]">{events.length}</span>
          )}
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-1.5 py-1 space-y-0.5">
        {subTab === 'agents' ? (
          <>
            {agents.length === 0 && (
              <p className="py-4 text-center text-[11px] text-muted-foreground">
                {connected ? 'No active agents' : 'Connecting…'}
              </p>
            )}
            {agents.map((agent) => (
              <AgentRow key={agent.agent_id} agent={agent} isStale={staleSet.has(agent.agent_id)} />
            ))}
          </>
        ) : (
          <>
            {events.length === 0 && (
              <p className="py-4 text-center text-[11px] text-muted-foreground">No events yet</p>
            )}
            {events.map((ev, i) => <EventRow key={i} ev={ev} />)}
          </>
        )}
      </div>

      {/* Pending approvals banner */}
      {(snapshot?.pending_tool_approvals.length ?? 0) > 0 && (
        <div className="mx-1.5 mb-1.5 rounded bg-red-500/10 border border-red-500/30 px-2 py-1">
          <p className="text-[10px] text-red-400 font-semibold">
            ⚠ {snapshot!.pending_tool_approvals.length} pending approval{snapshot!.pending_tool_approvals.length > 1 ? 's' : ''}
          </p>
        </div>
      )}
    </div>
  );
}

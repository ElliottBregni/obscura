import { ExternalLink } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useHealthSummary } from '@/api/hooks/useHealth';
import { Spinner } from '@/components/ui/Spinner';

const STATUS_DOT: Record<string, string> = {
  healthy: 'bg-emerald-500',
  warning: 'bg-yellow-500',
  critical: 'bg-red-500',
  unknown: 'bg-muted-foreground/40',
};


function timeAgo(ts: string | null): string {
  if (!ts) return 'never';
  const diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

export function FleetPanel() {
  const { data: summary, isLoading } = useHealthSummary();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Spinner className="h-4 w-4" />
      </div>
    );
  }

  if (!summary || summary.agents.length === 0) {
    return (
      <div className="px-3 py-6 text-center text-xs text-muted-foreground">
        No agents reporting heartbeats.
      </div>
    );
  }

  // Summary bar
  const counts = [
    { label: 'healthy', color: 'text-emerald-500', val: summary.healthy },
    { label: 'warn', color: 'text-yellow-500', val: summary.warning },
    { label: 'crit', color: 'text-red-500', val: summary.critical },
    { label: 'unknown', color: 'text-muted-foreground', val: summary.unknown },
  ].filter((c) => c.val > 0);

  return (
    <div className="flex flex-col gap-0.5">
      {/* Summary chips */}
      <div className="flex flex-wrap gap-1.5 px-2.5 py-2">
        {counts.map((c) => (
          <span
            key={c.label}
            className={`rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium ${c.color}`}
          >
            {c.val} {c.label}
          </span>
        ))}
      </div>

      {/* Agent rows */}
      {summary.agents.map((agent) => {
        const status = agent.status ?? 'unknown';
        const shortId =
          agent.agent_id.length > 20
            ? agent.agent_id.slice(0, 8) + '…'
            : agent.agent_id;

        return (
          <div
            key={agent.agent_id}
            className="group flex items-center gap-2 rounded-md px-2.5 py-1.5 hover:bg-muted transition-colors"
            title={agent.agent_id}
          >
            {/* Status dot */}
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[status] ?? STATUS_DOT.unknown}`}
            />

            {/* Agent info */}
            <div className="min-w-0 flex-1">
              <div className="truncate font-mono text-[11px] text-foreground">
                {shortId}
              </div>
              <div className="text-[10px] text-muted-foreground">
                {timeAgo(agent.last_heartbeat)}
                {agent.missed_count > 0 && (
                  <span className="ml-1 text-yellow-500">
                    · {agent.missed_count} missed
                  </span>
                )}
              </div>
            </div>

            {/* Link to health detail */}
            <Link
              to={`/health/${agent.agent_id}`}
              className="shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
              title="View health detail"
            >
              <ExternalLink className="h-3 w-3 text-muted-foreground hover:text-foreground" />
            </Link>
          </div>
        );
      })}
    </div>
  );
}

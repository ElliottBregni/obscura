import { Link, useParams } from 'react-router-dom';
import { useAgentHealth } from '@/api/hooks/useHealth';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Progress } from '@/components/ui/Progress';
import { Spinner } from '@/components/ui/Spinner';
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from '@/components/ui/Card';
import { ArrowLeft } from 'lucide-react';

export default function AgentHealthPage() {
  const { agentId } = useParams<{ agentId: string }>();
  const { data: health, isLoading, error } = useAgentHealth(agentId);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner size={32} />
      </div>
    );
  }

  if (error) {
    return (
      <p className="py-12 text-center text-sm text-red-500">
        Failed to load agent health: {(error as Error).message}
      </p>
    );
  }

  if (!health) return null;

  return (
    <div className="space-y-6">
      <Link
        to="/health"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Health
      </Link>

      {/* Header */}
      <div className="flex items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight font-mono">
            {health.agent_id}
          </h1>
          <p className="mt-1 text-muted-foreground">Agent health details</p>
        </div>
        <StatusBadge status={health.status} />
      </div>

      {/* Heartbeat Info */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Heartbeat</CardTitle>
          <CardDescription>
            Last heartbeat information and missed count.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-3">
            <div>
              <dt className="text-sm text-muted-foreground">
                Last Heartbeat
              </dt>
              <dd className="mt-1 font-mono text-sm">
                {health.last_heartbeat
                  ? new Date(health.last_heartbeat.timestamp).toLocaleString()
                  : '--'}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">
                Expected Interval
              </dt>
              <dd className="mt-1 font-mono text-sm">
                {health.expected_interval != null
                  ? `${health.expected_interval}s`
                  : '--'}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">Missed Count</dt>
              <dd className="mt-1 font-mono text-sm">
                {health.missed_count ?? 0}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* Resource Metrics */}
      {health.metrics && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Resource Metrics</CardTitle>
            <CardDescription>
              CPU, memory, and disk utilisation reported by the agent.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <MetricBar
              label="CPU"
              value={health.metrics.cpu_percent}
              suffix="%"
            />
            <MetricBar
              label="Memory"
              value={health.metrics.memory_percent}
              suffix="%"
            />
            {health.metrics.disk_usage != null && (
              <MetricBar
                label="Disk"
                value={health.metrics.disk_usage}
                suffix="%"
              />
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function MetricBar({
  label,
  value,
  suffix,
}: {
  label: string;
  value: number;
  suffix: string;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono font-medium">
          {value.toFixed(1)}
          {suffix}
        </span>
      </div>
      <Progress value={value} className="h-2" />
    </div>
  );
}

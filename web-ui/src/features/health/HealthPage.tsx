import { Link } from 'react-router-dom';
import { useHealthSummary } from '@/api/hooks/useHealth';
import { HealthRing } from '@/components/charts/HealthRing';
import { MetricCard } from '@/components/charts/MetricCard';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Spinner } from '@/components/ui/Spinner';
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
} from '@/components/ui/Card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table';
import {
  HeartPulse,
  AlertTriangle,
  AlertCircle,
  HelpCircle,
  Activity,
} from 'lucide-react';

export default function HealthPage() {
  const { data: summary, isLoading, error } = useHealthSummary();

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
        Failed to load health data: {(error as Error).message}
      </p>
    );
  }

  if (!summary) return null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Agent Health</h1>
        <p className="mt-1 text-muted-foreground">
          Heartbeat monitoring and health status for all agents.
        </p>
      </div>

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <MetricCard
          label="Total Agents"
          value={summary.total}
          icon={Activity}
        />
        <MetricCard
          label="Healthy"
          value={summary.healthy}
          icon={HeartPulse}
        />
        <MetricCard
          label="Warning"
          value={summary.warning}
          icon={AlertTriangle}
        />
        <MetricCard
          label="Critical"
          value={summary.critical}
          icon={AlertCircle}
        />
        <MetricCard
          label="Unknown"
          value={summary.unknown}
          icon={HelpCircle}
        />
      </div>

      {/* Health ring + agent table */}
      <div className="grid gap-6 lg:grid-cols-[auto_1fr]">
        <Card className="flex items-center justify-center p-6">
          <HealthRing
            healthy={summary.healthy}
            warning={summary.warning}
            critical={summary.critical}
            unknown={summary.unknown}
          />
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">All Agents</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Agent ID</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Last Heartbeat</TableHead>
                  <TableHead className="text-right">Missed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {summary.agents.length === 0 && (
                  <TableRow>
                    <TableCell
                      colSpan={4}
                      className="text-center text-muted-foreground"
                    >
                      No agents reporting.
                    </TableCell>
                  </TableRow>
                )}
                {summary.agents.map((agent) => (
                  <TableRow key={agent.agent_id}>
                    <TableCell>
                      <Link
                        to={`/health/${agent.agent_id}`}
                        className="font-mono text-sm text-primary hover:underline"
                      >
                        {agent.agent_id}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={agent.status} />
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {agent.last_heartbeat
                        ? new Date(agent.last_heartbeat).toLocaleString()
                        : '--'}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {agent.missed_count}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

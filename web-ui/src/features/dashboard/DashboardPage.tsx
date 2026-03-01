import { Link } from 'react-router-dom';
import {
  Bot,
  Database,
  GitBranch,
  ShieldCheck,
  Plus,
  Activity,
} from 'lucide-react';
import { useSystemMetrics } from '@/api/hooks/useMetrics';
import { useHealthSummary } from '@/api/hooks/useHealth';
import { useAuditLogs } from '@/api/hooks/useAudit';
import { MetricCard } from '@/components/charts/MetricCard';
import { HealthRing } from '@/components/charts/HealthRing';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { Skeleton } from '@/components/ui/Skeleton';
import { formatRelative } from '@/lib/utils';

export default function DashboardPage() {
  const { data: metrics, isLoading: metricsLoading } = useSystemMetrics();
  const { data: health, isLoading: healthLoading } = useHealthSummary();
  const { data: auditData, isLoading: auditLoading } = useAuditLogs(10, 0);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
          <p className="text-sm text-muted-foreground">
            System overview and quick actions
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button asChild size="sm">
            <Link to="/agents/spawn">
              <Plus className="mr-2 h-4 w-4" />
              Spawn Agent
            </Link>
          </Button>
          <Button asChild variant="outline" size="sm">
            <Link to="/workflows/create">
              <GitBranch className="mr-2 h-4 w-4" />
              Create Workflow
            </Link>
          </Button>
        </div>
      </div>

      {/* Metric cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {metricsLoading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24 rounded-lg" />
          ))
        ) : (
          <>
            <MetricCard
              label="Running Agents"
              value={metrics?.agents.by_status?.running ?? 0}
              icon={Bot}
              trendValue={`${metrics?.agents.total ?? 0} total`}
              trend="neutral"
            />
            <MetricCard
              label="Memory Keys"
              value={metrics?.memory.total_keys ?? 0}
              icon={Database}
              trendValue={`${Object.keys(metrics?.memory.namespaces ?? {}).length} namespaces`}
              trend="neutral"
            />
            <MetricCard
              label="Active Workflows"
              value={metrics?.workflows.total_executions ?? 0}
              icon={GitBranch}
              trendValue={`${metrics?.workflows.total_workflows ?? 0} total`}
              trend="neutral"
            />
            <MetricCard
              label="Pending Approvals"
              value={0}
              icon={ShieldCheck}
              trendValue="review queue"
              trend="neutral"
            />
          </>
        )}
      </div>

      {/* Health ring + recent audit */}
      <div className="grid gap-6 lg:grid-cols-3">
        {/* Health ring */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Agent Health</CardTitle>
          </CardHeader>
          <CardContent className="flex items-center justify-center py-4">
            {healthLoading ? (
              <Spinner className="h-8 w-8" />
            ) : health ? (
              <div className="flex flex-col items-center gap-4">
                <HealthRing
                  healthy={health.healthy}
                  warning={health.warning}
                  critical={health.critical}
                  unknown={health.unknown}
                />
                <div className="flex flex-wrap justify-center gap-3 text-xs">
                  <span className="flex items-center gap-1">
                    <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
                    Healthy ({health.healthy})
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="inline-block h-2 w-2 rounded-full bg-yellow-500" />
                    Warning ({health.warning})
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="inline-block h-2 w-2 rounded-full bg-red-500" />
                    Critical ({health.critical})
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="inline-block h-2 w-2 rounded-full bg-zinc-500" />
                    Unknown ({health.unknown})
                  </span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                No health data available
              </p>
            )}
          </CardContent>
        </Card>

        {/* Recent audit activity */}
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-base">Recent Activity</CardTitle>
            <Button asChild variant="ghost" size="sm">
              <Link to="/audit">View all</Link>
            </Button>
          </CardHeader>
          <CardContent>
            {auditLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-10 w-full rounded" />
                ))}
              </div>
            ) : auditData?.logs && auditData.logs.length > 0 ? (
              <div className="space-y-1">
                {auditData.logs.map((entry, i) => (
                  <div
                    key={`${entry.timestamp}-${i}`}
                    className="flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-muted/50"
                  >
                    <div className="flex items-center gap-3">
                      <Activity className="h-3.5 w-3.5 text-muted-foreground" />
                      <div>
                        <span className="font-medium">{entry.event_type}</span>
                        <span className="mx-1.5 text-muted-foreground">on</span>
                        <span className="text-muted-foreground">
                          {entry.resource}
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={
                          entry.outcome === 'success' ? 'default' : 'destructive'
                        }
                        className="text-[10px]"
                      >
                        {entry.outcome}
                      </Badge>
                      <span className="whitespace-nowrap text-xs text-muted-foreground">
                        {formatRelative(entry.timestamp)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="py-8 text-center text-sm text-muted-foreground">
                No recent activity
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

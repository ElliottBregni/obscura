import { useHealth, HealthSummary } from '@/api/client';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table';
import { Activity, Heart, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';

const statusConfig: Record<string, { dot: string; label: string; badge: 'success' | 'warning' | 'danger' | 'default' }> = {
  healthy: { dot: 'bg-emerald-500', label: 'Healthy', badge: 'success' },
  warning: { dot: 'bg-yellow-500', label: 'Warning', badge: 'warning' },
  critical: { dot: 'bg-red-500', label: 'Critical', badge: 'danger' },
  unknown: { dot: 'bg-muted-foreground', label: 'Unknown', badge: 'default' },
};

const defaultHealth: HealthSummary = { total: 0, healthy: 0, warning: 0, critical: 0, unknown: 0, agents: [] };

export function HealthDashboard() {
  const { data: healthData = defaultHealth, isLoading } = useHealth();

  const agents = healthData.agents ?? [];
  const healthy = healthData.healthy;
  const warning = healthData.warning;
  const critical = healthData.critical;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-semibold text-foreground">Health Monitoring</h1>
        <p className="text-sm text-muted-foreground mt-1">Monitor agent health and system status</p>
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: 'Total', value: healthData.total, color: 'text-foreground' },
          { label: 'Healthy', value: healthy, color: 'text-emerald-400' },
          { label: 'Warning', value: warning, color: 'text-yellow-400' },
          { label: 'Critical', value: critical, color: 'text-red-400' },
        ].map((stat) => (
          <Card key={stat.label}>
            <CardContent className="p-4 text-center">
              <div className={cn('text-2xl font-bold', stat.color)}>{stat.value}</div>
              <div className="text-sm text-muted-foreground">{stat.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Health Table */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Activity className="w-5 h-5 text-emerald-400" />
            Agent Health Status
          </CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : agents.length === 0 ? (
            <div className="text-center py-12">
              <Heart className="w-12 h-12 mx-auto mb-4 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">No health data available</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Status</TableHead>
                  <TableHead>Agent</TableHead>
                  <TableHead>Details</TableHead>
                  <TableHead className="text-right">State</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {agents.map((health) => {
                  const config = statusConfig[health.status] || statusConfig.unknown;
                  return (
                    <TableRow key={health.agent_id}>
                      <TableCell>
                        <div className={cn('w-2.5 h-2.5 rounded-full', config.dot)} />
                      </TableCell>
                      <TableCell className="font-medium text-foreground">
                        {health.agent_id}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {health.last_heartbeat
                          ? `Last heartbeat: ${new Date(health.last_heartbeat).toLocaleString()}`
                          : health.missed_count > 0
                            ? `Missed heartbeats: ${health.missed_count}`
                            : '\u2014'}
                      </TableCell>
                      <TableCell className="text-right">
                        <Badge variant={config.badge}>{config.label}</Badge>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

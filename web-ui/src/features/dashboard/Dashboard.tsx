import { useAgents, useMetrics, useHealth } from '@/api/client';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Separator } from '@/components/ui/Separator';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table';
import { Bot, Zap, Activity, Database, GitBranch, ArrowRight, Loader2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { formatDate } from '@/lib/utils';
import { useWebSocket } from '@/hooks/useWebSocket';

const stats = [
  { key: 'agents', label: 'Total Agents', icon: Bot },
  { key: 'running', label: 'Running', icon: Zap },
  { key: 'memory', label: 'Memory Keys', icon: Database },
  { key: 'workflows', label: 'Workflows', icon: GitBranch },
];

function StatCard({ label, value, icon: Icon }: { label: string; value: number; icon: React.ElementType }) {
  return (
    <Card>
      <CardContent className="p-6">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-muted-foreground">{label}</p>
            <p className="text-2xl font-semibold text-foreground mt-1">{value}</p>
          </div>
          <Icon className="h-4 w-4 text-muted-foreground" />
        </div>
      </CardContent>
    </Card>
  );
}

function getStatusBadge(status: string) {
  switch (status) {
    case 'running':
      return <Badge variant="success">running</Badge>;
    case 'completed':
      return <Badge variant="success">completed</Badge>;
    case 'error':
    case 'failed':
      return <Badge variant="danger">{status}</Badge>;
    case 'pending':
    case 'waiting':
      return <Badge variant="warning">{status}</Badge>;
    default:
      return <Badge variant="default">{status}</Badge>;
  }
}

export function Dashboard() {
  const { data: agents = [], isLoading: agentsLoading } = useAgents();
  const { data: metrics } = useMetrics();
  const { data: healthData } = useHealth();
  const { connected } = useWebSocket();

  const statValues = {
    agents: metrics?.agents?.total ?? agents.length,
    running: metrics?.agents?.running ?? agents.filter(a => a.status === 'running').length,
    memory: metrics?.memory?.total_keys ?? 0,
    workflows: metrics?.workflows?.total ?? 0,
  };

  const recentAgents = agents.slice(0, 5);

  const apiStatus = healthData ? 'Operational' : 'Unavailable';
  const wsStatus = connected ? 'Connected' : 'Disconnected';
  const memoryStatus = metrics?.memory ? 'Active' : 'Unknown';

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Dashboard</h1>
          <p className="text-sm text-muted-foreground mt-1">Overview of your Obscura instance</p>
        </div>
        <Link to="/agents/spawn">
          <Button leftIcon={<Zap className="w-4 h-4" />}>
            Spawn Agent
          </Button>
        </Link>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {stats.map((stat) => (
          <StatCard
            key={stat.key}
            label={stat.label}
            value={statValues[stat.key as keyof typeof statValues]}
            icon={stat.icon}
          />
        ))}
      </div>

      {/* Main Content */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Recent Agents */}
        <Card className="lg:col-span-2">
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>Recent Agents</CardTitle>
              <Link to="/agents">
                <Button variant="ghost" size="sm" rightIcon={<ArrowRight className="w-4 h-4" />}>
                  View All
                </Button>
              </Link>
            </div>
          </CardHeader>
          <CardContent>
            {agentsLoading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            ) : recentAgents.length === 0 ? (
              <div className="text-center py-12">
                <Bot className="w-10 h-10 mx-auto mb-3 text-muted-foreground" />
                <p className="text-sm text-muted-foreground">No agents yet</p>
                <Link to="/agents/spawn" className="text-sm text-primary hover:underline mt-2 inline-block">
                  Spawn your first agent
                </Link>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="hidden sm:table-cell">Created</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {recentAgents.map((agent) => (
                    <TableRow key={agent.id}>
                      <TableCell>
                        <div>
                          <p className="font-medium text-foreground">{agent.name}</p>
                          <p className="text-sm text-muted-foreground">
                            {agent.agent_id}{agent.model ? ` \u00b7 ${agent.model}` : ''}
                          </p>
                        </div>
                      </TableCell>
                      <TableCell>{getStatusBadge(agent.status)}</TableCell>
                      <TableCell className="hidden sm:table-cell text-sm text-muted-foreground">
                        {formatDate(agent.created_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* System Status */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Activity className="w-4 h-4 text-muted-foreground" />
                System Status
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-0">
              <div className="flex items-center justify-between py-3">
                <span className="text-sm text-muted-foreground">API Status</span>
                <Badge variant={apiStatus === 'Operational' ? 'success' : 'danger'}>
                  {apiStatus}
                </Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-sm text-muted-foreground">WebSocket</span>
                <Badge variant={wsStatus === 'Connected' ? 'success' : 'danger'}>
                  {wsStatus}
                </Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-sm text-muted-foreground">Memory Store</span>
                <Badge variant={memoryStatus === 'Active' ? 'success' : 'warning'}>
                  {memoryStatus}
                </Badge>
              </div>
              {metrics?.timestamp && (
                <>
                  <Separator />
                  <p className="text-xs text-muted-foreground pt-3">
                    Last updated: {formatDate(metrics.timestamp)}
                  </p>
                </>
              )}
            </CardContent>
          </Card>

          {/* Agent Summary */}
          <Card>
            <CardHeader>
              <CardTitle>Agent Summary</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-3">
                {[
                  { label: 'Running', count: metrics?.agents?.running ?? 0, dot: 'bg-emerald-500' },
                  { label: 'Idle', count: metrics?.agents?.idle ?? 0, dot: 'bg-blue-500' },
                  { label: 'Error', count: metrics?.agents?.error ?? 0, dot: 'bg-red-500' },
                ].map((item) => (
                  <div key={item.label} className="flex items-center gap-3">
                    <div className={`w-2 h-2 rounded-full ${item.dot}`} />
                    <span className="flex-1 text-sm text-foreground">{item.label}</span>
                    <span className="text-sm font-medium text-muted-foreground">{item.count}</span>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

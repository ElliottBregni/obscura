import { useState } from 'react';
import { useAgents, useStopAgent, Agent } from '@/api/client';
import { Card, CardContent } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table';
import { Bot, Plus, Pause, Play, Search, RefreshCw, Trash2, Loader2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { formatDate } from '@/lib/utils';

function getStatusBadge(status: string) {
  switch (status) {
    case 'running':
      return <Badge variant="success">Running</Badge>;
    case 'completed':
      return <Badge variant="success">Completed</Badge>;
    case 'idle':
      return <Badge variant="info">Idle</Badge>;
    case 'error':
    case 'failed':
      return <Badge variant="danger">{status.charAt(0).toUpperCase() + status.slice(1)}</Badge>;
    case 'pending':
    case 'waiting':
      return <Badge variant="warning">{status.charAt(0).toUpperCase() + status.slice(1)}</Badge>;
    default:
      return <Badge variant="default">{status.charAt(0).toUpperCase() + status.slice(1)}</Badge>;
  }
}

function AgentActions({ agent }: { agent: Agent }) {
  const stopAgent = useStopAgent();

  return (
    <div className="flex items-center gap-1">
      {agent.status === 'running' ? (
        <Button
          variant="ghost"
          size="icon"
          onClick={() => stopAgent.mutate(agent.id)}
          isLoading={stopAgent.isPending}
        >
          <Pause className="w-4 h-4" />
        </Button>
      ) : (
        <Button variant="ghost" size="icon">
          <Play className="w-4 h-4" />
        </Button>
      )}
      <Button
        variant="ghost"
        size="icon"
        onClick={() => stopAgent.mutate(agent.id)}
      >
        <Trash2 className="w-4 h-4 text-destructive" />
      </Button>
    </div>
  );
}

export function AgentsList() {
  const { data: agents = [], isLoading, refetch } = useAgents();
  const [filter, setFilter] = useState<string>('all');
  const [search, setSearch] = useState('');

  const filteredAgents = agents.filter(agent => {
    const matchesFilter = filter === 'all' || agent.status === filter;
    const matchesSearch = agent.name.toLowerCase().includes(search.toLowerCase()) ||
                         agent.agent_id.toLowerCase().includes(search.toLowerCase());
    return matchesFilter && matchesSearch;
  });

  const stats = {
    total: agents.length,
    running: agents.filter(a => a.status === 'running').length,
    idle: agents.filter(a => a.status === 'idle').length,
    error: agents.filter(a => a.status === 'error' || a.status === 'failed').length,
  };

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Agents</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {stats.total} total
            <span className="mx-1.5 text-muted-foreground/50">/</span>
            {stats.running} running
            <span className="mx-1.5 text-muted-foreground/50">/</span>
            {stats.idle} idle
            <span className="mx-1.5 text-muted-foreground/50">/</span>
            {stats.error} error
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" leftIcon={<RefreshCw className="w-4 h-4" />} onClick={() => refetch()}>
            Refresh
          </Button>
          <Link to="/agents/spawn">
            <Button size="sm" leftIcon={<Plus className="w-4 h-4" />}>
              Spawn Agent
            </Button>
          </Link>
        </div>
      </div>

      {/* Filters & Search */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <Tabs value={filter} onValueChange={setFilter}>
          <TabsList>
            <TabsTrigger value="all">All</TabsTrigger>
            <TabsTrigger value="running">Running</TabsTrigger>
            <TabsTrigger value="idle">Idle</TabsTrigger>
            <TabsTrigger value="error">Error</TabsTrigger>
            <TabsTrigger value="stopped">Stopped</TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="relative flex-1 sm:max-w-xs">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="Search agents..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
      </div>

      {/* Agents Table */}
      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : filteredAgents.length === 0 ? (
            <div className="text-center py-12">
              <Bot className="w-10 h-10 mx-auto mb-3 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">No agents found</p>
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
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredAgents.map((agent) => (
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
                    <TableCell className="text-right">
                      <AgentActions agent={agent} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

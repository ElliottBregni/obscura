import { useState, useMemo } from 'react';
import { Link } from 'react-router-dom';
import {
  Plus,
  LayoutTemplate,
  Users,
  MessageSquare,
  Square,
} from 'lucide-react';
import { useAgents, useStopAgent } from '@/api/hooks/useAgents';
import type { Agent } from '@/api/types';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { SearchInput } from '@/components/ui/SearchInput';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { formatRelative } from '@/lib/utils';

type StatusFilter = 'all' | Agent['status'];

const STATUS_FILTERS: { label: string; value: StatusFilter }[] = [
  { label: 'All', value: 'all' },
  { label: 'Running', value: 'running' },
  { label: 'Idle', value: 'idle' },
  { label: 'Error', value: 'error' },
  { label: 'Stopped', value: 'stopped' },
];

export default function AgentsPage() {
  const { data, isLoading, error } = useAgents();
  const stopAgent = useStopAgent();

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  const agents = data?.agents ?? [];

  const filtered = useMemo(() => {
    return agents.filter((agent) => {
      const matchesSearch = agent.name
        .toLowerCase()
        .includes(search.toLowerCase());
      const matchesStatus =
        statusFilter === 'all' || agent.status === statusFilter;
      return matchesSearch && matchesStatus;
    });
  }, [agents, search, statusFilter]);

  const handleStop = (agentId: string) => {
    stopAgent.mutate(agentId);
  };

  if (error) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-destructive">
          Failed to load agents: {String(error)}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Agents</h1>
          <p className="text-sm text-muted-foreground">
            Manage and monitor your agents
          </p>
        </div>
        <Button asChild size="sm">
          <Link to="/agents/spawn">
            <Plus className="mr-2 h-4 w-4" />
            Spawn Agent
          </Link>
        </Button>
      </div>

      {/* Sub-nav */}
      <div className="flex items-center gap-2">
        <Button asChild variant="outline" size="sm">
          <Link to="/agents/templates">
            <LayoutTemplate className="mr-2 h-4 w-4" />
            Templates
          </Link>
        </Button>
        <Button asChild variant="outline" size="sm">
          <Link to="/agents/groups">
            <Users className="mr-2 h-4 w-4" />
            Groups
          </Link>
        </Button>
      </div>

      {/* Filters */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
        <SearchInput
          value={search}
          onChange={setSearch}
          placeholder="Search agents by name..."
          className="w-full sm:max-w-xs"
        />
        <div className="flex flex-wrap gap-1">
          {STATUS_FILTERS.map((f) => (
            <Button
              key={f.value}
              variant={statusFilter === f.value ? 'default' : 'outline'}
              size="sm"
              onClick={() => setStatusFilter(f.value)}
              className="text-xs"
            >
              {f.label}
            </Button>
          ))}
        </div>
      </div>

      {/* Agent list */}
      {isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-40 rounded-lg" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
          <div className="text-center">
            <p className="text-sm text-muted-foreground">
              {agents.length === 0
                ? 'No agents spawned yet'
                : 'No agents match your filters'}
            </p>
            {agents.length === 0 && (
              <Button asChild variant="link" size="sm" className="mt-2">
                <Link to="/agents/spawn">Spawn your first agent</Link>
              </Button>
            )}
          </div>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((agent) => (
            <Card
              key={agent.agent_id}
              className="flex flex-col justify-between"
            >
              <CardContent className="p-4">
                <div className="flex items-start justify-between">
                  <div className="min-w-0 flex-1">
                    <Link
                      to={`/agents/${agent.agent_id}`}
                      className="text-sm font-semibold hover:underline"
                    >
                      {agent.name}
                    </Link>
                    <div className="mt-1 flex items-center gap-2">
                      <StatusBadge status={agent.status} />
                    </div>
                  </div>
                </div>

                <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                  {agent.model && (
                    <div className="flex items-center justify-between">
                      <span>Model</span>
                      <Badge variant="secondary" className="text-[10px]">
                        {agent.model}
                      </Badge>
                    </div>
                  )}
                  <div className="flex items-center justify-between">
                    <span>Iterations</span>
                    <span className="font-medium text-foreground">
                      {agent.iteration_count ?? 0}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Created</span>
                    <span>{formatRelative(agent.created_at)}</span>
                  </div>
                </div>

                <div className="mt-4 flex items-center gap-2">
                  <Button asChild variant="outline" size="sm" className="flex-1">
                    <Link to={`/agents/${agent.agent_id}/chat`}>
                      <MessageSquare className="mr-1.5 h-3.5 w-3.5" />
                      Chat
                    </Link>
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    className="flex-1"
                    disabled={
                      agent.status === 'stopped' || stopAgent.isPending
                    }
                    onClick={() => handleStop(agent.agent_id)}
                  >
                    <Square className="mr-1.5 h-3.5 w-3.5" />
                    Stop
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Count summary */}
      {!isLoading && agents.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Showing {filtered.length} of {agents.length} agents
        </p>
      )}
    </div>
  );
}

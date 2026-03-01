import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Plus,
  ChevronDown,
  ChevronRight,
  Trash2,
  Radio,
  Users,
} from 'lucide-react';
import {
  useAgentGroups,
  useCreateAgentGroup,
  useDeleteAgentGroup,
  useBroadcastToGroup,
  useAgents,
} from '@/api/hooks/useAgents';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Card, CardContent } from '@/components/ui/Card';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/Dialog';
import { Spinner } from '@/components/ui/Spinner';
import { Skeleton } from '@/components/ui/Skeleton';
import { cn, formatRelative } from '@/lib/utils';

export default function AgentGroupsPage() {
  const { data: groups, isLoading, error } = useAgentGroups();
  const { data: agentsData } = useAgents();
  const createGroup = useCreateAgentGroup();
  const deleteGroup = useDeleteAgentGroup();
  const broadcastToGroup = useBroadcastToGroup();

  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [broadcastDialogOpen, setBroadcastDialogOpen] = useState(false);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);

  // Create form state
  const [newGroupName, setNewGroupName] = useState('');
  const [selectedAgentIds, setSelectedAgentIds] = useState<string[]>([]);

  // Broadcast state
  const [broadcastMessage, setBroadcastMessage] = useState('');

  const agents = agentsData?.agents ?? [];

  const toggleExpand = (groupId: string) => {
    setExpandedGroup(expandedGroup === groupId ? null : groupId);
  };

  const toggleAgentSelection = (agentId: string) => {
    setSelectedAgentIds((prev) =>
      prev.includes(agentId)
        ? prev.filter((id) => id !== agentId)
        : [...prev, agentId]
    );
  };

  const handleCreate = () => {
    if (!newGroupName.trim()) return;
    createGroup.mutate(
      { name: newGroupName.trim(), agent_ids: selectedAgentIds },
      {
        onSuccess: () => {
          setCreateDialogOpen(false);
          setNewGroupName('');
          setSelectedAgentIds([]);
        },
      }
    );
  };

  const handleDelete = (id: string) => {
    deleteGroup.mutate(id, {
      onSuccess: () => setDeleteConfirmId(null),
    });
  };

  const openBroadcast = (groupId: string) => {
    setSelectedGroupId(groupId);
    setBroadcastMessage('');
    setBroadcastDialogOpen(true);
  };

  const handleBroadcast = () => {
    if (!selectedGroupId || !broadcastMessage.trim()) return;
    broadcastToGroup.mutate(
      { id: selectedGroupId, message: broadcastMessage.trim() },
      {
        onSuccess: () => {
          setBroadcastDialogOpen(false);
          setBroadcastMessage('');
          setSelectedGroupId(null);
        },
      }
    );
  };

  const getAgentName = (agentId: string) => {
    const agent = agents.find((a) => a.agent_id === agentId);
    return agent?.name ?? agentId;
  };

  if (error) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-destructive">
          Failed to load groups: {String(error)}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Agent Groups</h1>
          <p className="text-sm text-muted-foreground">
            Organize agents and broadcast messages
          </p>
        </div>
        <Button size="sm" onClick={() => setCreateDialogOpen(true)}>
          <Plus className="mr-2 h-4 w-4" />
          Create Group
        </Button>
      </div>

      {/* Group list */}
      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full rounded-lg" />
          ))}
        </div>
      ) : groups && groups.length > 0 ? (
        <div className="space-y-3">
          {groups.map((group) => {
            const isExpanded = expandedGroup === group.group_id;

            return (
              <Card key={group.group_id}>
                <CardContent className="p-0">
                  {/* Group header row */}
                  <div className="flex items-center justify-between px-4 py-3">
                    <button
                      type="button"
                      onClick={() => toggleExpand(group.group_id)}
                      className="flex items-center gap-3 text-left"
                    >
                      {isExpanded ? (
                        <ChevronDown className="h-4 w-4 text-muted-foreground" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-muted-foreground" />
                      )}
                      <div>
                        <div className="flex items-center gap-2">
                          <Users className="h-4 w-4 text-muted-foreground" />
                          <span className="text-sm font-semibold">
                            {group.name}
                          </span>
                        </div>
                        <div className="mt-0.5 flex items-center gap-2">
                          <Badge
                            variant="secondary"
                            className="text-[10px]"
                          >
                            {group.agent_ids.length} agent
                            {group.agent_ids.length !== 1 ? 's' : ''}
                          </Badge>
                          <span className="text-xs text-muted-foreground">
                            Created {formatRelative(group.created_at)}
                          </span>
                        </div>
                      </div>
                    </button>

                    <div className="flex items-center gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => openBroadcast(group.group_id)}
                        disabled={group.agent_ids.length === 0}
                      >
                        <Radio className="mr-1.5 h-3.5 w-3.5" />
                        Broadcast
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-destructive hover:text-destructive"
                        onClick={() => setDeleteConfirmId(group.group_id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </div>

                  {/* Expanded member list */}
                  {isExpanded && (
                    <div className="border-t border-border px-4 py-3">
                      {group.agent_ids.length > 0 ? (
                        <div className="space-y-1">
                          {group.agent_ids.map((agentId) => (
                            <Link
                              key={agentId}
                              to={`/agents/${agentId}`}
                              className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-muted/50"
                            >
                              <span>{getAgentName(agentId)}</span>
                              <span className="font-mono text-xs text-muted-foreground">
                                {agentId.slice(0, 8)}...
                              </span>
                            </Link>
                          ))}
                        </div>
                      ) : (
                        <p className="text-sm text-muted-foreground">
                          No agents in this group
                        </p>
                      )}
                    </div>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      ) : (
        <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
          <div className="text-center">
            <p className="text-sm text-muted-foreground">
              No groups created yet
            </p>
            <Button
              variant="link"
              size="sm"
              className="mt-2"
              onClick={() => setCreateDialogOpen(true)}
            >
              Create your first group
            </Button>
          </div>
        </div>
      )}

      {/* Create Group Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Agent Group</DialogTitle>
            <DialogDescription>
              Create a group to organize agents and broadcast messages.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="group-name">Group Name</Label>
              <Input
                id="group-name"
                value={newGroupName}
                onChange={(e) => setNewGroupName(e.target.value)}
                placeholder="research-team"
              />
            </div>

            <div className="space-y-2">
              <Label>Select Agents</Label>
              {agents.length > 0 ? (
                <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-border p-2">
                  {agents.map((agent) => (
                    <button
                      key={agent.agent_id}
                      type="button"
                      onClick={() => toggleAgentSelection(agent.agent_id)}
                      className={cn(
                        'flex w-full items-center justify-between rounded-md px-2 py-1.5 text-sm transition-colors',
                        selectedAgentIds.includes(agent.agent_id)
                          ? 'bg-primary/10 text-primary'
                          : 'hover:bg-muted/50'
                      )}
                    >
                      <span>{agent.name}</span>
                      {selectedAgentIds.includes(agent.agent_id) && (
                        <Badge
                          variant="default"
                          className="text-[10px]"
                        >
                          Selected
                        </Badge>
                      )}
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">
                  No agents available
                </p>
              )}
              {selectedAgentIds.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  {selectedAgentIds.length} agent
                  {selectedAgentIds.length !== 1 ? 's' : ''} selected
                </p>
              )}
            </div>
          </div>

          {createGroup.error && (
            <div className="rounded-md border border-destructive/20 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {String(createGroup.error)}
            </div>
          )}

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCreateDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button
              onClick={handleCreate}
              disabled={!newGroupName.trim() || createGroup.isPending}
            >
              {createGroup.isPending && <Spinner className="mr-2 h-4 w-4" />}
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Broadcast Dialog */}
      <Dialog
        open={broadcastDialogOpen}
        onOpenChange={setBroadcastDialogOpen}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Broadcast Message</DialogTitle>
            <DialogDescription>
              Send a message to all agents in this group.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label htmlFor="broadcast-msg">Message</Label>
            <textarea
              id="broadcast-msg"
              rows={4}
              value={broadcastMessage}
              onChange={(e) => setBroadcastMessage(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              placeholder="Enter your broadcast message..."
            />
          </div>

          {broadcastToGroup.error && (
            <div className="rounded-md border border-destructive/20 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {String(broadcastToGroup.error)}
            </div>
          )}

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setBroadcastDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button
              onClick={handleBroadcast}
              disabled={
                !broadcastMessage.trim() || broadcastToGroup.isPending
              }
            >
              {broadcastToGroup.isPending && (
                <Spinner className="mr-2 h-4 w-4" />
              )}
              <Radio className="mr-2 h-4 w-4" />
              Broadcast
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog
        open={!!deleteConfirmId}
        onOpenChange={() => setDeleteConfirmId(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Group</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this group? The agents will not be
              affected.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteConfirmId(null)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => deleteConfirmId && handleDelete(deleteConfirmId)}
              disabled={deleteGroup.isPending}
            >
              {deleteGroup.isPending && <Spinner className="mr-2 h-4 w-4" />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

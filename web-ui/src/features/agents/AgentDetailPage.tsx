import { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  MessageSquare,
  Square,
  Play,
  Tag,
  Wrench,
  Users,
  X,
  Plus,
} from 'lucide-react';
import {
  useAgent,
  useAgentTools,
  useAgentPeers,
  useAgentMessages,
  useStopAgent,
  useRunAgent,
  useAgentTags,
  useAddAgentTags,
  useRemoveAgentTags,
} from '@/api/hooks/useAgents';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Input } from '@/components/ui/Input';
import { Spinner } from '@/components/ui/Spinner';
import { Separator } from '@/components/ui/Separator';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/Dialog';
import { formatDate, formatRelative } from '@/lib/utils';

export default function AgentDetailPage() {
  const { agentId } = useParams<{ agentId: string }>();
  const { data: agent, isLoading, error } = useAgent(agentId);
  const { data: tools } = useAgentTools(agentId);
  const { data: peers } = useAgentPeers(agentId);
  const { data: messages } = useAgentMessages(agentId);
  const { data: tags } = useAgentTags(agentId);
  const stopAgent = useStopAgent();
  const runAgent = useRunAgent();
  const addTags = useAddAgentTags();
  const removeTags = useRemoveAgentTags();

  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [runInput, setRunInput] = useState('{}');
  const [newTag, setNewTag] = useState('');

  const handleStop = () => {
    if (agentId) stopAgent.mutate(agentId);
  };

  const handleRun = () => {
    if (!agentId) return;
    try {
      const input = JSON.parse(runInput);
      runAgent.mutate(
        { id: agentId, input },
        {
          onSuccess: () => setRunDialogOpen(false),
        }
      );
    } catch {
      // invalid JSON, ignore
    }
  };

  const handleAddTag = () => {
    if (!agentId || !newTag.trim()) return;
    addTags.mutate(
      { id: agentId, tags: [newTag.trim()] },
      { onSuccess: () => setNewTag('') }
    );
  };

  const handleRemoveTag = (tag: string) => {
    if (!agentId) return;
    removeTags.mutate({ id: agentId, tags: [tag] });
  };

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  if (error || !agent) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-destructive">
          {error ? `Failed to load agent: ${String(error)}` : 'Agent not found'}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{agent.name}</h1>
          <div className="mt-1 flex items-center gap-3">
            <StatusBadge status={agent.status} />
            {agent.model && (
              <Badge variant="secondary" className="text-xs">
                {agent.model}
              </Badge>
            )}
            <span className="text-xs text-muted-foreground">
              Created {formatDate(agent.created_at)}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button asChild variant="outline" size="sm">
            <Link to={`/agents/${agentId}/chat`}>
              <MessageSquare className="mr-2 h-4 w-4" />
              Chat
            </Link>
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setRunDialogOpen(true)}
          >
            <Play className="mr-2 h-4 w-4" />
            Run
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={agent.status === 'stopped' || stopAgent.isPending}
            onClick={handleStop}
          >
            <Square className="mr-2 h-4 w-4" />
            Stop
          </Button>
        </div>
      </div>

      <Separator />

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Left column: details */}
        <div className="space-y-6 lg:col-span-2">
          {/* Tags */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Tag className="h-4 w-4" />
                Tags
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap items-center gap-2">
                {(tags ?? agent.tags ?? []).map((tag) => (
                  <Badge
                    key={tag}
                    variant="secondary"
                    className="flex items-center gap-1"
                  >
                    {tag}
                    <button
                      type="button"
                      onClick={() => handleRemoveTag(tag)}
                      className="ml-0.5 hover:text-destructive"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </Badge>
                ))}
                <form
                  className="flex items-center gap-1"
                  onSubmit={(e) => {
                    e.preventDefault();
                    handleAddTag();
                  }}
                >
                  <Input
                    value={newTag}
                    onChange={(e) => setNewTag(e.target.value)}
                    placeholder="Add tag..."
                    className="h-7 w-28 text-xs"
                  />
                  <Button
                    type="submit"
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    disabled={!newTag.trim() || addTags.isPending}
                  >
                    <Plus className="h-3.5 w-3.5" />
                  </Button>
                </form>
              </div>
            </CardContent>
          </Card>

          {/* Tools */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Wrench className="h-4 w-4" />
                Tools
                {tools && (
                  <Badge variant="secondary" className="ml-auto text-[10px]">
                    {tools.length}
                  </Badge>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {tools && tools.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {tools.map((tool) => (
                    <Badge key={tool} variant="outline">
                      {tool}
                    </Badge>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No tools assigned
                </p>
              )}
            </CardContent>
          </Card>

          {/* Messages */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Recent Messages</CardTitle>
            </CardHeader>
            <CardContent>
              {messages && messages.length > 0 ? (
                <div className="space-y-3">
                  {messages.slice(-10).map((msg, i) => (
                    <div
                      key={i}
                      className="rounded-md border border-border p-3"
                    >
                      <div className="mb-1 flex items-center gap-2">
                        <Badge
                          variant={
                            msg.role === 'user' ? 'default' : 'secondary'
                          }
                          className="text-[10px]"
                        >
                          {msg.role}
                        </Badge>
                      </div>
                      <p className="whitespace-pre-wrap text-sm text-foreground">
                        {msg.content}
                      </p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No messages yet
                </p>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right column: peers + info */}
        <div className="space-y-6">
          {/* Agent info */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">ID</span>
                <span className="font-mono text-xs">{agent.agent_id}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Status</span>
                <StatusBadge status={agent.status} />
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Iterations</span>
                <span>{agent.iteration_count ?? 0}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">MCP</span>
                <span>{agent.mcp_enabled ? 'Enabled' : 'Disabled'}</span>
              </div>
              {agent.updated_at && (
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Updated</span>
                  <span className="text-xs">
                    {formatRelative(agent.updated_at)}
                  </span>
                </div>
              )}
              {agent.error_message && (
                <div className="mt-2 rounded border border-destructive/20 bg-destructive/10 p-2 text-xs text-destructive">
                  {agent.error_message}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Peers */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Users className="h-4 w-4" />
                Peers
                {peers && (
                  <Badge variant="secondary" className="ml-auto text-[10px]">
                    {peers.length}
                  </Badge>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {peers && peers.length > 0 ? (
                <div className="space-y-1">
                  {peers.map((peerId) => (
                    <Link
                      key={peerId}
                      to={`/agents/${peerId}`}
                      className="block rounded-md px-2 py-1.5 text-sm hover:bg-muted/50"
                    >
                      <span className="font-mono text-xs">{peerId}</span>
                    </Link>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No connected peers
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* Run dialog */}
      <Dialog open={runDialogOpen} onOpenChange={setRunDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Run Agent</DialogTitle>
            <DialogDescription>
              Provide input JSON for the agent run.
            </DialogDescription>
          </DialogHeader>
          <div>
            <textarea
              value={runInput}
              onChange={(e) => setRunInput(e.target.value)}
              rows={6}
              className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              placeholder='{"key": "value"}'
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRunDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button
              onClick={handleRun}
              disabled={runAgent.isPending}
            >
              {runAgent.isPending ? (
                <Spinner className="mr-2 h-4 w-4" />
              ) : (
                <Play className="mr-2 h-4 w-4" />
              )}
              Run
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

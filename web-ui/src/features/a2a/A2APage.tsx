import { useState } from 'react';
import {
  useA2AAgentCard,
  useA2ATasks,
  useA2ACreateTask,
} from '@/api/hooks/useA2A';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Spinner } from '@/components/ui/Spinner';
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/Dialog';
import type { A2ATask } from '@/api/types';

export default function A2APage() {
  const agentCard = useA2AAgentCard();
  const tasks = useA2ATasks();
  const createTask = useA2ACreateTask();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [message, setMessage] = useState('');
  const [selectedTask, setSelectedTask] = useState<A2ATask | null>(null);

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!message.trim()) return;
    createTask.mutate(
      { message: message.trim() },
      {
        onSuccess: () => {
          setMessage('');
          setDialogOpen(false);
        },
      }
    );
  };

  const isLoading = agentCard.isLoading || tasks.isLoading;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner size={32} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">A2A</h1>
        <p className="mt-1 text-muted-foreground">
          Agent-to-Agent protocol. View the agent card, browse tasks, and create
          new ones.
        </p>
      </div>

      {/* Agent Card */}
      {agentCard.error && (
        <p className="text-sm text-red-500">
          Failed to load agent card: {(agentCard.error as Error).message}
        </p>
      )}
      {agentCard.data && (
        <Card>
          <CardHeader>
            <CardTitle>{agentCard.data.name}</CardTitle>
            <CardDescription>{agentCard.data.description}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">URL:</span>
              <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                {agentCard.data.url}
              </code>
            </div>
            {agentCard.data.capabilities.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {agentCard.data.capabilities.map((cap) => (
                  <Badge key={cap} variant="secondary">
                    {cap}
                  </Badge>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Tasks Section */}
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">Tasks</h2>
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogTrigger asChild>
            <Button>Create Task</Button>
          </DialogTrigger>
          <DialogContent>
            <form onSubmit={handleCreate}>
              <DialogHeader>
                <DialogTitle>Create Task</DialogTitle>
                <DialogDescription>
                  Send a message to create a new A2A task.
                </DialogDescription>
              </DialogHeader>
              <div className="py-4">
                <textarea
                  className="flex min-h-[120px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                  placeholder="Enter your message..."
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                />
              </div>
              {createTask.isError && (
                <p className="mb-2 text-sm text-red-500">
                  {(createTask.error as Error).message}
                </p>
              )}
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setDialogOpen(false)}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={createTask.isPending}>
                  {createTask.isPending ? 'Sending...' : 'Send'}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      {tasks.error && (
        <p className="text-sm text-red-500">
          Failed to load tasks: {(tasks.error as Error).message}
        </p>
      )}

      {tasks.data && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Task ID</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tasks.data.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={4}
                    className="text-center text-muted-foreground"
                  >
                    No tasks yet.
                  </TableCell>
                </TableRow>
              )}
              {tasks.data.map((task) => (
                <TableRow
                  key={task.task_id}
                  className="cursor-pointer"
                  onClick={() =>
                    setSelectedTask(
                      selectedTask?.task_id === task.task_id ? null : task
                    )
                  }
                >
                  <TableCell className="font-mono text-xs">
                    {task.task_id}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={task.status} />
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {new Date(task.created_at).toLocaleString()}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {task.updated_at
                      ? new Date(task.updated_at).toLocaleString()
                      : '--'}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {/* Task Detail */}
      {selectedTask && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg font-mono">
              {selectedTask.task_id}
            </CardTitle>
            <CardDescription>
              {selectedTask.messages.length} message
              {selectedTask.messages.length !== 1 ? 's' : ''}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {selectedTask.messages.map((msg, idx) => (
              <div
                key={idx}
                className="rounded-md border bg-muted/40 p-3 text-sm"
              >
                <span className="mb-1 block text-xs font-medium text-muted-foreground">
                  {msg.role}
                </span>
                <p className="whitespace-pre-wrap">{msg.content}</p>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

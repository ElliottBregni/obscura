import { useState, useCallback } from 'react';
import { toast } from 'sonner';
import { MonitorSmartphone, Plus, Trash2 } from 'lucide-react';
import {
  useSessions,
  useCreateSession,
  useDeleteSession,
} from '@/api/hooks/useSessions';
import { BACKENDS } from '@/lib/constants';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Label } from '@/components/ui/Label';
import { Card, CardContent } from '@/components/ui/Card';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/Table';
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/Select';
import { Spinner } from '@/components/ui/Spinner';
import { EmptyState } from '@/components/ui/EmptyState';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/Dialog';
import { formatDate } from '@/lib/utils';

export default function SessionsPage() {
  const sessionsQuery = useSessions();
  const createSession = useCreateSession();
  const deleteSession = useDeleteSession();

  const [createOpen, setCreateOpen] = useState(false);
  const [selectedBackend, setSelectedBackend] = useState<string>(BACKENDS[0].value);

  const handleCreate = useCallback(() => {
    createSession.mutate(
      { backend: selectedBackend },
      {
        onSuccess: (session) => {
          toast.success(`Session ${session.session_id} created`);
          setCreateOpen(false);
        },
        onError: (err) => toast.error(`Create failed: ${String(err)}`),
      },
    );
  }, [createSession, selectedBackend]);

  const handleDelete = useCallback(
    (sessionId: string) => {
      if (!confirm(`Delete session ${sessionId}?`)) return;
      deleteSession.mutate(sessionId, {
        onSuccess: () => toast.success('Session deleted'),
        onError: (err) => toast.error(`Delete failed: ${String(err)}`),
      });
    },
    [deleteSession],
  );

  if (sessionsQuery.isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (sessionsQuery.isError) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-destructive">
          Failed to load sessions.
        </CardContent>
      </Card>
    );
  }

  const sessions = sessionsQuery.data ?? [];

  const backendLabel = (value: string) =>
    BACKENDS.find((b) => b.value === value)?.label ?? value;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <MonitorSmartphone className="h-6 w-6 text-primary" />
          <h1 className="text-2xl font-bold tracking-tight">Sessions</h1>
          <Badge variant="secondary">{sessions.length}</Badge>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="mr-1.5 h-4 w-4" />
          Create Session
        </Button>
      </div>

      {/* Table */}
      {!sessions.length ? (
        <EmptyState
          icon={MonitorSmartphone}
          title="No Sessions"
          description="Create a session to start interacting with an agent backend."
          action={
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="mr-1.5 h-4 w-4" />
              Create Session
            </Button>
          }
        />
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Session ID</TableHead>
                <TableHead>Backend</TableHead>
                <TableHead>Created</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sessions.map((session) => (
                <TableRow key={session.session_id}>
                  <TableCell className="font-mono text-sm">
                    {session.session_id}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">
                      {backendLabel(session.backend)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDate(session.created_at)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8"
                      onClick={() => handleDelete(session.session_id)}
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Session</DialogTitle>
            <DialogDescription>
              Select a backend to create a new session.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label>Backend</Label>
            <Select value={selectedBackend} onValueChange={setSelectedBackend}>
              <SelectTrigger>
                <SelectValue placeholder="Select a backend" />
              </SelectTrigger>
              <SelectContent>
                {BACKENDS.map((b) => (
                  <SelectItem key={b.value} value={b.value}>
                    {b.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={createSession.isPending}>
              {createSession.isPending ? (
                <Spinner size={16} className="mr-2" />
              ) : null}
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

import { useState, useCallback } from 'react';
import { toast } from 'sonner';
import { MonitorSmartphone, Plus, Trash2, ChevronDown, ChevronRight, Copy, Check } from 'lucide-react';
import {
  useSessions,
  useCreateSession,
  useDeleteSession,
  useIngestSessions,
} from '@/api/hooks/useSessions';
import { useSessionBroadcastSync } from '@/hooks/useSessionBroadcastSync';
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
import type { Session } from '@/api/types';

// Backend session_id may contain a Python repr string like
// "SessionMetadata(sessionId='uuid', startTime='...', ...)" — extract fields.
function cleanSessionId(raw: string): string {
  const match = raw.match(/sessionId='([^']+)'/);
  return match ? match[1] : raw;
}

function extractReprField(raw: string, field: string): string | null {
  const re = new RegExp(`${field}='([^']*)'`);
  const match = raw.match(re);
  return match ? match[1] : null;
}

function SessionDetailPanel({ session }: { session: Session }) {
  const [copied, setCopied] = useState(false);
  const rawId = session.session_id;
  const displayId = cleanSessionId(rawId);
  const isRepr = rawId !== displayId;

  const startTime = isRepr ? extractReprField(rawId, 'startTime') : null;

  const handleCopy = () => {
    navigator.clipboard.writeText(displayId);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/30 p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <p className="text-xs font-medium text-muted-foreground">Session ID</p>
          <div className="mt-0.5 flex items-center gap-2">
            <code className="text-sm font-mono">{displayId}</code>
            <Button variant="ghost" size="icon" className="h-6 w-6" onClick={handleCopy}>
              {copied ? <Check className="h-3 w-3 text-emerald-500" /> : <Copy className="h-3 w-3" />}
            </Button>
          </div>
        </div>
        <div>
          <p className="text-xs font-medium text-muted-foreground">Backend</p>
          <p className="mt-0.5 text-sm">{session.backend}</p>
        </div>
        <div>
          <p className="text-xs font-medium text-muted-foreground">Source</p>
          <p className="mt-0.5 text-sm">{session.source ?? 'live'}</p>
        </div>
        {startTime && (
          <div>
            <p className="text-xs font-medium text-muted-foreground">Start Time</p>
            <p className="mt-0.5 text-sm">{startTime}</p>
          </div>
        )}
      </div>
      {isRepr && (
        <div>
          <p className="text-xs font-medium text-muted-foreground">Raw Metadata</p>
          <pre className="mt-1 overflow-auto rounded-md bg-background p-2 text-xs text-muted-foreground">
            {rawId}
          </pre>
        </div>
      )}
    </div>
  );
}

export default function SessionsPage() {
  useSessionBroadcastSync(true);
  const sessionsQuery = useSessions();
  const createSession = useCreateSession();
  const deleteSession = useDeleteSession();
  const ingestSessions = useIngestSessions();

  const [createOpen, setCreateOpen] = useState(false);
  const [selectedBackend, setSelectedBackend] = useState<string>(BACKENDS[0].value);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<'all' | 'live' | 'ingested'>('all');

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
    (sessionId: string, e: React.MouseEvent) => {
      e.stopPropagation();
      if (!confirm(`Delete session ${cleanSessionId(sessionId)}?`)) return;
      deleteSession.mutate(sessionId, {
        onSuccess: () => {
          toast.success('Session deleted');
          if (expandedId === sessionId) setExpandedId(null);
        },
        onError: (err) => toast.error(`Delete failed: ${String(err)}`),
      });
    },
    [deleteSession, expandedId],
  );

  const handleIngest = useCallback(() => {
    ingestSessions.mutate(
      {},
      {
        onSuccess: (result) => {
          toast.success(
            `Ingested ${result.ingested} sessions (${result.skipped} skipped, ${result.entries} indexed)`,
          );
        },
        onError: (err) => toast.error(`Ingest failed: ${String(err)}`),
      },
    );
  }, [ingestSessions]);

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
  const filteredSessions =
    sourceFilter === 'all'
      ? sessions
      : sessions.filter((s) => (s.source ?? 'live') === sourceFilter);

  const backendLabel = (value: string) =>
    BACKENDS.find((b) => b.value === value)?.label ?? value;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <MonitorSmartphone className="h-6 w-6 text-primary" />
          <h1 className="text-2xl font-bold tracking-tight">Sessions</h1>
          <Badge variant="secondary">{filteredSessions.length}</Badge>
        </div>
        <div className="flex items-center gap-2">
          <Select
            value={sourceFilter}
            onValueChange={(value) =>
              setSourceFilter(value as 'all' | 'live' | 'ingested')
            }
          >
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="Filter source" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Sessions</SelectItem>
              <SelectItem value="live">Live Sessions</SelectItem>
              <SelectItem value="ingested">Ingested Sessions</SelectItem>
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            onClick={handleIngest}
            disabled={ingestSessions.isPending}
          >
            {ingestSessions.isPending ? 'Ingesting...' : 'Ingest System Sessions'}
          </Button>
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="mr-1.5 h-4 w-4" />
            Create Session
          </Button>
        </div>
      </div>

      {/* Table */}
      {!filteredSessions.length ? (
        <EmptyState
          icon={MonitorSmartphone}
          title="No Sessions In View"
          description="Try a different source filter or ingest system sessions."
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
                <TableHead className="w-8" />
                <TableHead>Session ID</TableHead>
                <TableHead>Backend</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredSessions.map((session) => {
                const isExpanded = expandedId === session.session_id;
                return (
                  <>
                    <TableRow
                      key={session.session_id}
                      className="cursor-pointer hover:bg-muted/50"
                      onClick={() =>
                        setExpandedId(isExpanded ? null : session.session_id)
                      }
                    >
                      <TableCell className="w-8 pr-0">
                        {isExpanded ? (
                          <ChevronDown className="h-4 w-4 text-muted-foreground" />
                        ) : (
                          <ChevronRight className="h-4 w-4 text-muted-foreground" />
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-sm">
                        {cleanSessionId(session.session_id)}
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary">
                          {backendLabel(session.backend)}
                        </Badge>
                        <Badge variant="outline" className="ml-2">
                          {session.source ?? 'live'}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          onClick={(e) => handleDelete(session.session_id, e)}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </TableCell>
                    </TableRow>
                    {isExpanded && (
                      <TableRow key={`${session.session_id}-detail`}>
                        <TableCell colSpan={4} className="p-3">
                          <SessionDetailPanel session={session} />
                        </TableCell>
                      </TableRow>
                    )}
                  </>
                );
              })}
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

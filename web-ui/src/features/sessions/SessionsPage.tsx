import { useState } from 'react';
import { Plus, Download, MessageSquare, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/Dialog';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import {
  useSessions,
  useCreateSession,
  useDeleteSession,
  useIngestSessions,
} from '@/api/hooks/useSessions';
import { BACKENDS } from '@/lib/constants';
import { useObserveStream } from '@/hooks/useObserveStream';
import { SessionChatView } from './components/SessionChatView';
import { KairosStatusDot } from './components/KairosStatusDot';
import { FleetPanel } from './components/FleetPanel';
import { ObservePanel } from './components/ObservePanel';
import { GoalsWidget } from './components/GoalsWidget';
import type { Session } from '@/api/types';

// ─── Session list item ────────────────────────────────────────────────────────

function SessionListItem({
  session,
  selected,
  onSelect,
  onDelete,
}: {
  session: Session;
  selected: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const rawId = session.session_id;
  const isUuid = /^[0-9a-f-]{36}$/i.test(rawId);
  const label = isUuid
    ? rawId.slice(0, 8) + '…'
    : rawId.length > 28
    ? rawId.slice(0, 24) + '…'
    : rawId;

  const backendLabel =
    BACKENDS.find((b) => b.value === session.backend)?.label ?? session.backend;

  return (
    <div
      onClick={onSelect}
      className={`group flex cursor-pointer items-start gap-2 rounded-md px-2.5 py-2 text-sm transition-colors
        ${selected
          ? 'bg-accent text-accent-foreground'
          : 'text-muted-foreground hover:bg-muted hover:text-foreground'
        }`}
    >
      <MessageSquare className="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-60" />
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-[11px]">{label}</div>
        <div className="mt-0.5 text-[10px] opacity-60">{backendLabel}</div>
      </div>
      <button
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
        className="ml-auto shrink-0 rounded p-0.5 opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
        title="Delete session"
      >
        <Trash2 className="h-3 w-3" />
      </button>
    </div>
  );
}

// ─── Create session dialog ────────────────────────────────────────────────────

function CreateSessionDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [sessionId, setSessionId] = useState('');
  const [backend, setBackend] = useState<string>(BACKENDS[0].value);
  const createSession = useCreateSession();

  const handleCreate = () => {
    if (!sessionId.trim()) return;
    createSession.mutate({ backend }, { onSuccess: onClose });
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New Session</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="session-id">Session ID</Label>
            <Input
              id="session-id"
              placeholder="e.g. my-agent-run"
              value={sessionId}
              onChange={(e) => setSessionId(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              autoFocus
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="backend">Backend</Label>
            <Select value={backend} onValueChange={setBackend}>
              <SelectTrigger id="backend"><SelectValue /></SelectTrigger>
              <SelectContent>
                {BACKENDS.map((b) => (
                  <SelectItem key={b.value} value={b.value}>{b.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={handleCreate} disabled={!sessionId.trim() || createSession.isPending}>
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ─── Empty state ──────────────────────────────────────────────────────────────

function EmptyState({ onNew }: { onNew: () => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
      <MessageSquare className="h-12 w-12 text-muted-foreground/30" />
      <div>
        <p className="text-sm font-medium text-muted-foreground">No session selected</p>
        <p className="mt-1 text-xs text-muted-foreground/60">
          Select a session from the sidebar or create a new one.
        </p>
      </div>
      <Button size="sm" onClick={onNew}>
        <Plus className="mr-1.5 h-3.5 w-3.5" />
        New Session
      </Button>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

type SidebarTab = 'sessions' | 'fleet' | 'runtime';

export default function SessionsPage() {
  const { data: sessions = [], isLoading } = useSessions();
  const deleteSession = useDeleteSession();
  const ingestSessions = useIngestSessions();
  const { staleCount } = useObserveStream(true);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('sessions');

  const selectedSession = sessions.find((s) => s.session_id === selectedId) ?? null;

  if (!selectedId && sessions.length > 0) {
    setSelectedId(sessions[0].session_id);
  }

  const handleDelete = (session: Session) => {
    deleteSession.mutate(session.session_id, {
      onSuccess: () => {
        if (selectedId === session.session_id) setSelectedId(null);
      },
    });
  };

  return (
    <div className="flex h-full overflow-hidden">
      {/* ── Sidebar ── */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-border bg-background">
        {/* Tabs */}
        <div className="flex items-center border-b border-border">
          {(['sessions', 'fleet', 'runtime'] as SidebarTab[]).map((tab) => (
            <button
              key={tab}
              onClick={() => setSidebarTab(tab)}
              className={`relative flex-1 py-2.5 text-[11px] font-semibold uppercase tracking-wider transition-colors
                ${sidebarTab === tab
                  ? 'border-b-2 border-primary text-foreground'
                  : 'text-muted-foreground hover:text-foreground'}`}
            >
              {tab === 'runtime' ? 'Live' : tab.charAt(0).toUpperCase() + tab.slice(1)}
              {tab === 'runtime' && staleCount > 0 && (
                <span className="absolute -top-0.5 right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-amber-500 text-[8px] font-bold text-black">
                  {staleCount}
                </span>
              )}
            </button>
          ))}
          {sidebarTab === 'sessions' && (
            <Button
              size="icon"
              variant="ghost"
              className="mr-1.5 h-6 w-6 shrink-0"
              onClick={() => setCreateOpen(true)}
              title="New session"
            >
              <Plus className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto py-1.5 px-1.5 space-y-0.5">
          {sidebarTab === 'fleet' && <FleetPanel />}
          {sidebarTab === 'runtime' && <ObservePanel />}
          {sidebarTab === 'sessions' && (
            <>
              {isLoading && (
                <p className="px-2 py-4 text-center text-xs text-muted-foreground">Loading…</p>
              )}
              {!isLoading && sessions.length === 0 && (
                <p className="px-2 py-4 text-center text-xs text-muted-foreground">No sessions yet.</p>
              )}
              {sessions.map((session) => (
                <SessionListItem
                  key={session.session_id}
                  session={session}
                  selected={session.session_id === selectedId}
                  onSelect={() => setSelectedId(session.session_id)}
                  onDelete={() => handleDelete(session)}
                />
              ))}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="border-t border-border">
          <GoalsWidget />
          <div className="p-2 space-y-1">
            <KairosStatusDot />
            <Button
              variant="ghost"
              size="sm"
              className="w-full justify-start text-xs text-muted-foreground"
              onClick={() => ingestSessions.mutate(undefined)}
              disabled={ingestSessions.isPending}
            >
              <Download className="mr-1.5 h-3 w-3" />
              {ingestSessions.isPending ? 'Ingesting…' : 'Ingest Sessions'}
            </Button>
          </div>
        </div>
      </aside>

      {/* ── Chat panel ── */}
      <main className="flex flex-1 flex-col overflow-hidden">
        {selectedSession ? (
          <SessionChatView key={selectedSession.session_id} session={selectedSession} />
        ) : (
          <EmptyState onNew={() => setCreateOpen(true)} />
        )}
      </main>

      <CreateSessionDialog open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}

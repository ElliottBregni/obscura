import { useEffect, useState, useRef } from 'react';
import { MessageSquare, Search } from 'lucide-react';
import { useSessions } from '@/api/hooks/useSessions';
import { BACKENDS } from '@/lib/constants';
import type { Session } from '@/api/types';

interface Props {
  open: boolean;
  onClose: () => void;
  onSelect: (session: Session) => void;
  currentId: string | null;
}

export function SessionCommandPalette({ open, onClose, onSelect, currentId }: Props) {
  const { data: sessions = [] } = useSessions();
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = sessions.filter((s) =>
    s.session_id.toLowerCase().includes(query.toLowerCase()) ||
    s.backend.toLowerCase().includes(query.toLowerCase())
  );

  // Focus input when opened
  useEffect(() => {
    if (open) {
      setQuery('');
      setCursor(0);
      setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  // Keyboard nav
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose(); }
      if (e.key === 'ArrowDown') { e.preventDefault(); setCursor((c) => Math.min(c + 1, filtered.length - 1)); }
      if (e.key === 'ArrowUp') { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
      if (e.key === 'Enter' && filtered[cursor]) { e.preventDefault(); onSelect(filtered[cursor]); onClose(); }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, filtered, cursor, onClose, onSelect]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[20vh]">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />

      {/* Palette */}
      <div className="relative w-full max-w-lg rounded-xl border border-border bg-card shadow-2xl overflow-hidden">
        {/* Search input */}
        <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setCursor(0); }}
            placeholder="Search sessions…"
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          <kbd className="hidden rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground sm:inline">
            ESC
          </kbd>
        </div>

        {/* Results */}
        <div className="max-h-72 overflow-y-auto py-1">
          {filtered.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">No sessions found</p>
          )}
          {filtered.map((session, i) => {
            const rawId = session.session_id;
            const isUuid = /^[0-9a-f-]{36}$/i.test(rawId);
            const label = isUuid ? rawId.slice(0, 8) + '…' : rawId.length > 32 ? rawId.slice(0, 28) + '…' : rawId;
            const backendLabel = BACKENDS.find((b) => b.value === session.backend)?.label ?? session.backend;
            const isCurrent = session.session_id === currentId;

            return (
              <button
                key={session.session_id}
                onClick={() => { onSelect(session); onClose(); }}
                className={`flex w-full items-center gap-3 px-3 py-2 text-left transition-colors
                  ${i === cursor ? 'bg-accent' : 'hover:bg-muted'}`}
                onMouseEnter={() => setCursor(i)}
              >
                <MessageSquare className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-xs text-foreground">{label}</div>
                  <div className="text-[10px] text-muted-foreground">{backendLabel}</div>
                </div>
                {isCurrent && (
                  <span className="shrink-0 rounded-full bg-primary/20 px-1.5 py-0.5 text-[10px] text-primary">
                    current
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <div className="border-t border-border px-3 py-1.5 flex items-center gap-3 text-[10px] text-muted-foreground">
          <span><kbd className="rounded bg-muted px-1 py-0.5">↑↓</kbd> navigate</span>
          <span><kbd className="rounded bg-muted px-1 py-0.5">↵</kbd> select</span>
          <span><kbd className="rounded bg-muted px-1 py-0.5">esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}

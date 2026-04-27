import { useEffect, useRef, useState } from 'react';
import { AlertCircle, RotateCcw, Settings } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { useChatStore } from '@/stores/chatStore';
import { useAgentStream } from '@/hooks/useAgentStream';
import { fetchApi } from '@/api/client';
import { BACKENDS } from '@/lib/constants';
import { ChatMessageBubble } from './ChatMessage';
import { ChatComposer } from './ChatComposer';
import { ToolApprovalBanner } from './ToolApprovalBanner';
import { SettingsDrawer } from './SettingsDrawer';
import type { Session } from '@/api/types';

interface Props {
  session: Session;
}

function useResumeSession(sessionId: string) {
  return useMutation({
    mutationFn: () =>
      fetchApi(`/api/v1/sessions/${sessionId}/resume`, { method: 'POST' }),
  });
}

export function SessionChatView({ session }: Props) {
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const error = useChatStore((s) => s.error);
  const clearMessages = useChatStore((s) => s.clearMessages);
  const setSessionId = useChatStore((s) => s.setSessionId);

  const backendValue = BACKENDS.find((b) => b.value === session.backend)
    ? session.backend
    : BACKENDS[0].value;

  const { sendMessage, cancelStream } = useAgentStream({
    backend: backendValue,
    sessionId: session.session_id,
  });

  const resumeSession = useResumeSession(session.session_id);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Sync session into store + clear on session change
  useEffect(() => {
    clearMessages();
    setSessionId(session.session_id);
  }, [session.session_id, clearMessages, setSessionId]);

  // Auto-scroll to bottom on new content
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Esc cancels stream (only when not in a modal)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isStreaming && !settingsOpen) {
        e.preventDefault();
        cancelStream();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isStreaming, cancelStream, settingsOpen]);

  const lastMsgId = messages[messages.length - 1]?.id;

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5 text-sm">
        <span className="font-mono text-xs text-muted-foreground truncate">
          {session.session_id.length > 40
            ? session.session_id.slice(0, 8) + '…' + session.session_id.slice(-8)
            : session.session_id}
        </span>
        <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
          {BACKENDS.find((b) => b.value === session.backend)?.label ?? session.backend}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={() => resumeSession.mutate()}
            disabled={resumeSession.isPending}
            title="Resume session"
            className="flex items-center gap-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors disabled:opacity-50"
          >
            <RotateCcw className={`h-3 w-3 ${resumeSession.isPending ? 'animate-spin' : ''}`} />
            Resume
          </button>
          <button
            onClick={() => setSettingsOpen(true)}
            title="Settings"
            className="rounded p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            <Settings className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {messages.length === 0 && (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Send a message to start the conversation.
          </div>
        )}

        {messages.map((msg) => (
          <ChatMessageBubble
            key={msg.id}
            msg={msg}
            isStreamingThis={isStreaming && msg.id === lastMsgId && msg.role === 'assistant'}
          />
        ))}

        {/* Error */}
        {error && (
          <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3.5 py-2.5 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {error}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Tool approval banner */}
      <ToolApprovalBanner />

      {/* Composer */}
      <ChatComposer
        onSend={sendMessage}
        onStop={cancelStream}
        isStreaming={isStreaming}
      />

      {/* Settings drawer */}
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}

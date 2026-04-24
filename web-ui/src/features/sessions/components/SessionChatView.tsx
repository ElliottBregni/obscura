import { useEffect, useRef, useState } from 'react';
import { Bot, User, Brain, AlertCircle, Loader2 } from 'lucide-react';
import { useChatStore } from '@/stores/chatStore';
import { useAgentStream } from '@/hooks/useAgentStream';
import { BACKENDS } from '@/lib/constants';
import { ToolCallBlock } from './ToolCallBlock';
import { ChatComposer } from './ChatComposer';
import { ToolApprovalBanner } from './ToolApprovalBanner';
import type { Session } from '@/api/types';

interface Props {
  session: Session;
}

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)} className="my-1.5 rounded-md border border-dashed border-border bg-muted/20 px-3 py-1.5 text-xs text-muted-foreground">
      <summary className="flex cursor-pointer items-center gap-1.5 font-medium select-none">
        <Brain className="h-3 w-3" />
        Thinking
      </summary>
      <pre className="mt-2 whitespace-pre-wrap break-words text-[11px] leading-relaxed opacity-80">
        {text}
      </pre>
    </details>
  );
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

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5 text-sm">
        <span className="font-mono text-xs text-muted-foreground truncate">
          {session.session_id.length > 40
            ? session.session_id.slice(0, 8) + '…' + session.session_id.slice(-8)
            : session.session_id}
        </span>
        <span className="ml-auto shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
          {BACKENDS.find((b) => b.value === session.backend)?.label ?? session.backend}
        </span>
      </div>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {messages.length === 0 && (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Send a message to start the conversation.
          </div>
        )}

        {messages.map((msg) => (
          <div key={msg.id} className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
            {/* Avatar */}
            <div className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold
              ${msg.role === 'user'
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground border border-border'}`}>
              {msg.role === 'user' ? <User className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
            </div>

            {/* Bubble */}
            <div className={`max-w-[80%] space-y-1 ${msg.role === 'user' ? 'items-end' : 'items-start'} flex flex-col`}>
              {/* Thinking */}
              {msg.thinking && <ThinkingBlock text={msg.thinking} />}

              {/* Tool calls */}
              {msg.toolCalls.map((tc) => (
                <ToolCallBlock key={tc.id} toolCall={tc} />
              ))}

              {/* Text */}
              {msg.text && (
                <div className={`rounded-lg px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap
                  ${msg.role === 'user'
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-muted/50 text-foreground border border-border/50'}`}>
                  {msg.text}
                  {/* streaming cursor */}
                  {isStreaming && msg.role === 'assistant' &&
                    msg.id === messages[messages.length - 1]?.id && (
                    <span className="ml-0.5 inline-block h-4 w-0.5 bg-current animate-pulse align-middle" />
                  )}
                </div>
              )}

              {/* Empty assistant message still streaming */}
              {msg.role === 'assistant' && !msg.text && !msg.thinking && msg.toolCalls.length === 0 && isStreaming && (
                <div className="flex items-center gap-2 rounded-lg border border-border/50 bg-muted/50 px-3.5 py-2.5">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                  <span className="text-sm text-muted-foreground">Thinking…</span>
                </div>
              )}
            </div>
          </div>
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

      {/* Tool approval banner */}
      <ToolApprovalBanner />

      {/* Composer */}
      <ChatComposer
        onSend={sendMessage}
        onStop={cancelStream}
        isStreaming={isStreaming}
      />
    </div>
  );
}

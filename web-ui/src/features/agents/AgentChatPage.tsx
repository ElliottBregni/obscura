import { useState, useRef, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Send,
  Square,
  Trash2,
  ChevronDown,
  ChevronRight,
  Wrench,
  Brain,
  Copy,
  Check,
  User,
  Sparkles,
} from 'lucide-react';
import { useAgentStream } from '@/hooks/useAgentStream';
import { useChatStore, type ChatMessage, type ToolCall } from '@/stores/chatStore';
import { useAgent } from '@/api/hooks/useAgents';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Thinking block
// ---------------------------------------------------------------------------
function ThinkingBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="mb-3 overflow-hidden rounded-lg border border-border/60 bg-black/20">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <Brain className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        <span className="font-medium">Thinking</span>
        <span className="ml-auto text-[10px] opacity-60">
          {open ? 'hide' : 'show'}
        </span>
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
      </button>
      {open && (
        <div className="border-t border-border/60 px-3 py-2.5">
          <pre className="whitespace-pre-wrap font-mono text-xs text-muted-foreground leading-relaxed">
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tool call card
// ---------------------------------------------------------------------------
function ToolCallCard({ toolCall }: { toolCall: ToolCall }) {
  const [inputOpen, setInputOpen] = useState(false);
  const [resultOpen, setResultOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async (text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, []);

  const statusColor =
    toolCall.status === 'complete'
      ? 'text-emerald-400'
      : toolCall.status === 'error'
        ? 'text-red-400'
        : 'text-amber-400';

  return (
    <div className="tool-card animate-in">
      <div className="tool-card-header">
        <Wrench className="h-3.5 w-3.5 text-primary/70 shrink-0" />
        <span className="text-foreground/90">{toolCall.name}</span>
        <span className={cn('ml-auto text-[10px] font-normal', statusColor)}>
          {toolCall.status}
        </span>
      </div>

      {toolCall.input && (
        <div className="border-b border-border/50">
          <button
            type="button"
            onClick={() => setInputOpen((v) => !v)}
            className="flex w-full items-center gap-1.5 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            {inputOpen ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            <span>Input</span>
          </button>
          {inputOpen && (
            <div className="relative px-3 pb-2.5">
              <pre className="overflow-auto font-mono text-xs text-muted-foreground leading-relaxed max-h-48">
                {toolCall.input}
              </pre>
              <button
                type="button"
                onClick={() => handleCopy(toolCall.input ?? '')}
                className="absolute right-3 top-0 p-1 text-muted-foreground hover:text-foreground"
              >
                {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
              </button>
            </div>
          )}
        </div>
      )}

      {toolCall.result && (
        <div>
          <button
            type="button"
            onClick={() => setResultOpen((v) => !v)}
            className="flex w-full items-center gap-1.5 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            {resultOpen ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            <span>Result</span>
          </button>
          {resultOpen && (
            <div className="px-3 pb-2.5">
              <pre className="overflow-auto font-mono text-xs text-muted-foreground leading-relaxed max-h-48">
                {toolCall.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Message bubble
// ---------------------------------------------------------------------------
function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';

  return (
    <div className={cn('group flex gap-3 animate-in', isUser ? 'flex-row-reverse' : 'flex-row')}>
      {/* Avatar */}
      <div
        className={cn(
          'mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold',
          isUser
            ? 'bg-primary/20 text-primary'
            : 'bg-primary/10 text-primary/80',
        )}
      >
        {isUser ? <User className="h-3.5 w-3.5" /> : <Sparkles className="h-3.5 w-3.5" />}
      </div>

      {/* Content */}
      <div className={cn('flex max-w-[82%] flex-col gap-1', isUser ? 'items-end' : 'items-start')}>
        {/* Timestamp */}
        {message.timestamp && (
          <span className="px-1 text-[10px] text-muted-foreground/60">
            {new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        )}

        {/* Thinking */}
        {!isUser && message.thinking && (
          <div className="w-full">
            <ThinkingBlock content={message.thinking} />
          </div>
        )}

        {/* Tool calls */}
        {!isUser && message.toolCalls.length > 0 && (
          <div className="w-full max-w-lg space-y-1">
            {message.toolCalls.map((tc) => (
              <ToolCallCard key={tc.id} toolCall={tc} />
            ))}
          </div>
        )}

        {/* Text */}
        {message.text && (
          <div className={cn(isUser ? 'bubble-user' : 'bubble-agent', 'max-w-lg')}>
            {isUser ? (
              <p className="text-sm leading-relaxed whitespace-pre-wrap">{message.text}</p>
            ) : (
              <div className="prose-chat prose prose-sm prose-invert max-w-none text-sm">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {message.text}
                </ReactMarkdown>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Typing indicator
// ---------------------------------------------------------------------------
function TypingIndicator() {
  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10">
        <Sparkles className="h-3.5 w-3.5 text-primary/80" />
      </div>
      <div className="bubble-agent flex items-center gap-1 py-3">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------
function EmptyState({ agentName }: { agentName: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-4 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 glow-primary">
        <Sparkles className="h-7 w-7 text-primary" />
      </div>
      <div>
        <p className="text-base font-semibold text-foreground">{agentName}</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Ready — send a message to get started
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2 mt-2">
        {['What can you do?', 'Show me your tools', 'Run a quick check'].map((hint) => (
          <button
            key={hint}
            type="button"
            className="rounded-full border border-border px-3 py-1.5 text-xs text-muted-foreground hover:border-primary/40 hover:text-foreground transition-colors"
          >
            {hint}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function AgentChatPage() {
  const { agentId } = useParams<{ agentId: string }>();
  const { data: agent } = useAgent(agentId);
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const chatError = useChatStore((s) => s.error);
  const clearMessages = useChatStore((s) => s.clearMessages);

  const { sendMessage, cancelStream } = useAgentStream({
    backend: 'copilot',
    agentId,
  });

  const [input, setInput] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Only auto-scroll if user is near the bottom
    const threshold = 80;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    if (nearBottom || isStreaming) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages, isStreaming]);

  const handleSend = useCallback(() => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput('');
    sendMessage(text);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [input, isStreaming, sendMessage]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleInput = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);

  const agentName = agent?.name ?? 'Agent';
  const showTyping =
    isStreaming &&
    (messages.length === 0 ||
      (!messages[messages.length - 1]?.text &&
        messages[messages.length - 1]?.toolCalls.length === 0));

  return (
    <div className="flex h-full flex-col bg-background">
      {/* \u2500\u2500 Chat header \u2500\u2500 */}
      <div
        className="flex h-12 shrink-0 items-center justify-between border-b px-4"
        style={{ borderColor: 'hsl(var(--border))' }}
      >
        <div className="flex items-center gap-2.5">
          <span className="text-sm font-semibold">{agentName}</span>
          {agent && <StatusBadge status={agent.status} />}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={clearMessages}
          disabled={messages.length === 0}
          className="h-7 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
        >
          <Trash2 className="h-3.5 w-3.5" />
          Clear
        </Button>
      </div>

      {/* \u2500\u2500 Messages \u2500\u2500 */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <EmptyState agentName={agentName} />
        ) : (
          <div className="mx-auto max-w-3xl space-y-5 px-4 py-6">
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} />
            ))}

            {showTyping && <TypingIndicator />}

            {chatError && (
              <div className="rounded-xl border border-destructive/20 bg-destructive/8 px-4 py-3 text-sm text-destructive">
                {chatError}
              </div>
            )}
          </div>
        )}
      </div>

      {/* \u2500\u2500 Input bar \u2500\u2500 */}
      <div
        className="shrink-0 border-t px-4 py-3"
        style={{ borderColor: 'hsl(var(--border))' }}
      >
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            placeholder={isStreaming ? 'Responding…' : 'Message agent…'}
            rows={1}
            disabled={isStreaming}
            className="chat-input flex-1"
          />
          {isStreaming ? (
            <button
              type="button"
              onClick={cancelStream}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-destructive/90 text-white transition-all hover:bg-destructive active:scale-95"
              aria-label="Stop"
            >
              <Square className="h-3.5 w-3.5 fill-current" />
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSend}
              disabled={!input.trim()}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground transition-all hover:bg-primary/90 active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed"
              aria-label="Send"
            >
              <Send className="h-4 w-4" />
            </button>
          )}
        </div>
        <p className="mx-auto mt-1.5 max-w-3xl text-center text-[10px] text-muted-foreground/50">
          Enter to send · Shift+Enter for newline
        </p>
      </div>
    </div>
  );
}

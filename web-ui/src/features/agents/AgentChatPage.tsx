import { useState, useRef, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Send,
  XCircle,
  Trash2,
  ChevronDown,
  ChevronRight,
  Wrench,
  Brain,
} from 'lucide-react';
import { useAgentStream } from '@/hooks/useAgentStream';
import { useChatStore, type ChatMessage, type ToolCall } from '@/stores/chatStore';
import { useAgent } from '@/api/hooks/useAgents';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { cn } from '@/lib/utils';

function ThinkingBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="my-2 rounded-md border border-border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
      >
        <Brain className="h-3.5 w-3.5" />
        <span>Thinking</span>
        {open ? (
          <ChevronDown className="ml-auto h-3.5 w-3.5" />
        ) : (
          <ChevronRight className="ml-auto h-3.5 w-3.5" />
        )}
      </button>
      {open && (
        <div className="border-t border-border px-3 py-2">
          <pre className="whitespace-pre-wrap text-xs text-muted-foreground">
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}

function ToolCallCard({ toolCall }: { toolCall: ToolCall }) {
  const [inputOpen, setInputOpen] = useState(false);
  const [resultOpen, setResultOpen] = useState(false);

  return (
    <Card className="my-2 overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border bg-muted/30 px-3 py-2">
        <Wrench className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-xs font-semibold">{toolCall.name}</span>
        <Badge
          variant={
            toolCall.status === 'complete'
              ? 'default'
              : toolCall.status === 'error'
                ? 'destructive'
                : 'secondary'
          }
          className="ml-auto text-[10px]"
        >
          {toolCall.status}
        </Badge>
      </div>

      {/* Input */}
      {toolCall.input && (
        <div className="border-b border-border">
          <button
            type="button"
            onClick={() => setInputOpen(!inputOpen)}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <span>Input</span>
            {inputOpen ? (
              <ChevronDown className="ml-auto h-3 w-3" />
            ) : (
              <ChevronRight className="ml-auto h-3 w-3" />
            )}
          </button>
          {inputOpen && (
            <pre className="overflow-auto px-3 pb-2 text-xs text-muted-foreground">
              {toolCall.input}
            </pre>
          )}
        </div>
      )}

      {/* Result */}
      {toolCall.result && (
        <div>
          <button
            type="button"
            onClick={() => setResultOpen(!resultOpen)}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <span>Result</span>
            {resultOpen ? (
              <ChevronDown className="ml-auto h-3 w-3" />
            ) : (
              <ChevronRight className="ml-auto h-3 w-3" />
            )}
          </button>
          {resultOpen && (
            <pre className="overflow-auto px-3 pb-2 text-xs text-muted-foreground">
              {toolCall.result}
            </pre>
          )}
        </div>
      )}
    </Card>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';

  return (
    <div
      className={cn('flex w-full', isUser ? 'justify-end' : 'justify-start')}
    >
      <div
        className={cn(
          'max-w-[85%] rounded-lg px-4 py-3',
          isUser
            ? 'bg-primary text-primary-foreground'
            : 'bg-muted/50 text-foreground'
        )}
      >
        {/* Thinking */}
        {!isUser && message.thinking && (
          <ThinkingBlock content={message.thinking} />
        )}

        {/* Tool calls */}
        {!isUser &&
          message.toolCalls.map((tc) => (
            <ToolCallCard key={tc.id} toolCall={tc} />
          ))}

        {/* Text content */}
        {message.text && (
          <div className="prose prose-sm prose-invert max-w-none">
            {isUser ? (
              <p className="mb-0 whitespace-pre-wrap">{message.text}</p>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {message.text}
              </ReactMarkdown>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function StreamingDots() {
  return (
    <div className="flex items-center gap-1 px-4 py-2">
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:0ms]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:150ms]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:300ms]" />
    </div>
  );
}

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

  // Auto-scroll on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSend = () => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput('');
    sendMessage(text);
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleTextareaInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    // Auto-resize
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
      {/* Chat header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold">
            {agent?.name ?? 'Agent Chat'}
          </h1>
          {agent && <StatusBadge status={agent.status} />}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={clearMessages}
          disabled={messages.length === 0}
        >
          <Trash2 className="mr-2 h-4 w-4" />
          Clear
        </Button>
      </div>

      {/* Messages area */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
          {messages.length === 0 ? (
            <div className="flex h-64 items-center justify-center">
              <div className="text-center">
                <p className="text-sm text-muted-foreground">
                  Start a conversation with{' '}
                  <span className="font-medium text-foreground">
                    {agent?.name ?? 'this agent'}
                  </span>
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Type a message below and press Enter to send
                </p>
              </div>
            </div>
          ) : (
            messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} />
            ))
          )}

          {/* Streaming indicator */}
          {isStreaming &&
            messages.length > 0 &&
            !messages[messages.length - 1].text &&
            messages[messages.length - 1].toolCalls.length === 0 && (
              <div className="flex justify-start">
                <div className="rounded-lg bg-muted/50">
                  <StreamingDots />
                </div>
              </div>
            )}

          {/* Error */}
          {chatError && (
            <div className="rounded-md border border-destructive/20 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              {chatError}
            </div>
          )}
        </div>
      </div>

      {/* Input area */}
      <div className="border-t border-border bg-background px-4 py-3">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleTextareaInput}
            onKeyDown={handleKeyDown}
            placeholder="Type a message..."
            rows={1}
            disabled={isStreaming}
            className="flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          />
          {isStreaming ? (
            <Button
              variant="destructive"
              size="icon"
              onClick={cancelStream}
              className="h-10 w-10 shrink-0"
            >
              <XCircle className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              size="icon"
              onClick={handleSend}
              disabled={!input.trim()}
              className="h-10 w-10 shrink-0"
            >
              <Send className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

import { useEffect, useRef } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, Bot, Loader2, Trash2 } from 'lucide-react';
import { useAgent } from '@/api/client';
import { useChatStore } from '@/stores/chatStore';
import { useAgentStream } from '@/hooks/useAgentStream';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { ChatMessage } from './chat/ChatMessage';
import { ChatInput } from './chat/ChatInput';

export function AgentChat() {
  const { agentId } = useParams<{ agentId: string }>();
  const { data: agent, isLoading: agentLoading } = useAgent(agentId || '');
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const error = useChatStore((s) => s.error);
  const clearMessages = useChatStore((s) => s.clearMessages);

  const { sendMessage, cancelStream } = useAgentStream({
    backend: agent?.model ? 'claude' : 'copilot',
    model: agent?.model,
  });

  // Auto-scroll
  const scrollRef = useRef<HTMLDivElement>(null);
  const isAutoScroll = useRef(true);

  useEffect(() => {
    if (isAutoScroll.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    isAutoScroll.current = atBottom;
  };

  // Clear messages on agent change
  useEffect(() => {
    clearMessages();
  }, [agentId, clearMessages]);

  if (agentLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3">
        <Bot className="w-10 h-10 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">Agent not found</p>
        <Link to="/agents" className="text-sm text-primary hover:underline">
          Back to agents
        </Link>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-border bg-background/80 backdrop-blur-sm">
        <Link to="/agents">
          <Button variant="ghost" size="icon" className="h-8 w-8">
            <ArrowLeft className="w-4 h-4" />
          </Button>
        </Link>
        <Bot className="w-5 h-5 text-primary" />
        <div className="flex-1 min-w-0">
          <h1 className="text-sm font-medium text-foreground truncate">
            {agent.name}
          </h1>
          <p className="text-xs text-muted-foreground">
            {agent.model || 'default model'}
          </p>
        </div>
        <Badge
          variant={agent.status === 'running' ? 'success' : 'default'}
        >
          {agent.status}
        </Badge>
        {messages.length > 0 && (
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={clearMessages}
          >
            <Trash2 className="w-4 h-4 text-muted-foreground" />
          </Button>
        )}
      </div>

      {/* Messages */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto"
      >
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3">
            <Bot className="w-12 h-12 opacity-20" />
            <p className="text-sm">Start a conversation with {agent.name}</p>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto px-4 py-6 space-y-6">
            {messages.map((msg) => (
              <ChatMessage key={msg.id} message={msg} />
            ))}
          </div>
        )}
      </div>

      {/* Error banner */}
      {error && (
        <div className="px-4 py-2 bg-destructive/10 text-destructive text-xs text-center">
          {error}
        </div>
      )}

      {/* Input */}
      <ChatInput
        onSend={sendMessage}
        onCancel={cancelStream}
        isStreaming={isStreaming}
      />
    </div>
  );
}

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { User, Bot } from 'lucide-react';
import type { ChatMessage as ChatMessageType } from '@/stores/chatStore';
import { ThinkingBlock } from './ThinkingBlock';
import { ToolCallCard } from './ToolCallCard';

interface ChatMessageProps {
  message: ChatMessageType;
}

export function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === 'user';

  return (
    <div className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}>
      {/* Avatar */}
      {!isUser && (
        <div className="flex-shrink-0 w-7 h-7 rounded-full bg-primary/10 flex items-center justify-center mt-1">
          <Bot className="w-4 h-4 text-primary" />
        </div>
      )}

      {/* Message body */}
      <div
        className={`max-w-[80%] ${
          isUser
            ? 'bg-primary text-primary-foreground rounded-2xl rounded-br-md px-4 py-2.5'
            : 'min-w-0'
        }`}
      >
        {isUser ? (
          <p className="text-sm whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div className="space-y-0">
            {/* Thinking block */}
            <ThinkingBlock text={message.thinking} isStreaming={message.isStreaming} />

            {/* Tool calls (before/between text) */}
            {message.toolCalls.map((tool) => (
              <ToolCallCard key={tool.id} tool={tool} />
            ))}

            {/* Main content */}
            {message.content && (
              <div className="prose prose-sm prose-invert max-w-none text-foreground [&_pre]:bg-background [&_pre]:rounded-lg [&_pre]:p-3 [&_pre]:text-xs [&_code]:text-xs [&_code]:bg-background [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded [&_p]:leading-relaxed [&_p]:mb-2 [&_ul]:mb-2 [&_ol]:mb-2 [&_li]:mb-0.5">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {message.content}
                </ReactMarkdown>
              </div>
            )}

            {/* Streaming cursor */}
            {message.isStreaming && !message.content && message.toolCalls.length === 0 && (
              <div className="flex items-center gap-1 text-muted-foreground">
                <span className="w-2 h-4 bg-muted-foreground/50 animate-pulse rounded-sm" />
              </div>
            )}
          </div>
        )}
      </div>

      {/* User avatar */}
      {isUser && (
        <div className="flex-shrink-0 w-7 h-7 rounded-full bg-primary flex items-center justify-center mt-1">
          <User className="w-4 h-4 text-primary-foreground" />
        </div>
      )}
    </div>
  );
}

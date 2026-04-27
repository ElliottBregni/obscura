import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Bot, User, Loader2 } from 'lucide-react';
import { ToolCallBlock } from './ToolCallBlock';
import type { ChatMessage as ChatMessageType } from '@/stores/chatStore';

interface ThinkingBlockProps { text: string }
function ThinkingBlock({ text }: ThinkingBlockProps) {
  return (
    <details className="my-1.5 rounded-md border border-dashed border-border bg-muted/20 px-3 py-1.5 text-xs text-muted-foreground">
      <summary className="flex cursor-pointer items-center gap-1.5 font-medium select-none">
        <span className="text-[10px]">◈</span> Thinking
      </summary>
      <pre className="mt-2 whitespace-pre-wrap break-words text-[11px] leading-relaxed opacity-80">{text}</pre>
    </details>
  );
}

interface Props {
  msg: ChatMessageType;
  isStreamingThis: boolean;
}

export function ChatMessageBubble({ msg, isStreamingThis }: Props) {
  const isUser = msg.role === 'user';

  return (
    <div className={`flex gap-3 ${isUser ? 'flex-row-reverse' : ''}`}>
      {/* Avatar */}
      <div className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold
        ${isUser ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground border border-border'}`}>
        {isUser ? <User className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
      </div>

      {/* Content */}
      <div className={`max-w-[80%] space-y-1 ${isUser ? 'items-end' : 'items-start'} flex flex-col`}>
        {msg.thinking && <ThinkingBlock text={msg.thinking} />}

        {msg.toolCalls.map((tc) => (
          <ToolCallBlock key={tc.id} toolCall={tc} />
        ))}

        {msg.text && (
          <div className={`rounded-lg px-3.5 py-2.5 text-sm leading-relaxed
            ${isUser
              ? 'bg-primary text-primary-foreground whitespace-pre-wrap'
              : 'bg-muted/50 text-foreground border border-border/50'}`}>
            {isUser ? (
              <span className="whitespace-pre-wrap">{msg.text}</span>
            ) : (
              <div className="prose prose-sm prose-invert max-w-none
                prose-p:my-1 prose-p:leading-relaxed
                prose-headings:mt-3 prose-headings:mb-1
                prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:font-mono prose-code:before:content-none prose-code:after:content-none
                prose-pre:bg-muted prose-pre:border prose-pre:border-border prose-pre:rounded-md prose-pre:p-3 prose-pre:my-2
                prose-ul:my-1 prose-ol:my-1 prose-li:my-0
                prose-blockquote:border-l-2 prose-blockquote:border-border prose-blockquote:pl-3 prose-blockquote:text-muted-foreground
                prose-table:text-xs prose-th:text-muted-foreground prose-td:border prose-td:border-border prose-td:px-2 prose-td:py-1
                prose-a:text-primary prose-a:no-underline hover:prose-a:underline">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {msg.text}
                </ReactMarkdown>
                {isStreamingThis && (
                  <span className="ml-0.5 inline-block h-4 w-0.5 bg-current animate-pulse align-middle" />
                )}
              </div>
            )}
          </div>
        )}

        {/* Empty assistant still streaming */}
        {!isUser && !msg.text && !msg.thinking && msg.toolCalls.length === 0 && isStreamingThis && (
          <div className="flex items-center gap-2 rounded-lg border border-border/50 bg-muted/50 px-3.5 py-2.5">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="text-sm text-muted-foreground">Thinking…</span>
          </div>
        )}
      </div>
    </div>
  );
}

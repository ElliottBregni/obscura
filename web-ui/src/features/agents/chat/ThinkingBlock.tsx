import { useState } from 'react';
import { ChevronRight, Brain } from 'lucide-react';

interface ThinkingBlockProps {
  text: string;
  isStreaming?: boolean;
}

export function ThinkingBlock({ text, isStreaming }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(false);

  if (!text) return null;

  const preview = text.length > 60 ? text.slice(0, 60) + '...' : text;

  return (
    <div className="mb-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronRight
          className={`w-3 h-3 transition-transform ${expanded ? 'rotate-90' : ''}`}
        />
        <Brain className="w-3 h-3" />
        {expanded ? 'Thinking' : preview}
        {isStreaming && (
          <span className="ml-1 inline-block w-1.5 h-1.5 rounded-full bg-muted-foreground animate-pulse" />
        )}
      </button>
      {expanded && (
        <div className="mt-2 pl-5 text-xs text-muted-foreground italic whitespace-pre-wrap leading-relaxed border-l border-border">
          {text}
        </div>
      )}
    </div>
  );
}

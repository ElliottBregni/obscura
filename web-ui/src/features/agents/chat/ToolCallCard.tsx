import { useState } from 'react';
import { ChevronRight, Loader2, CheckCircle2, XCircle, Wrench } from 'lucide-react';
import type { ToolCallInfo } from '@/stores/chatStore';

interface ToolCallCardProps {
  tool: ToolCallInfo;
}

export function ToolCallCard({ tool }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);

  const statusIcon = {
    running: <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-400" />,
    complete: <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />,
    error: <XCircle className="w-3.5 h-3.5 text-red-400" />,
  }[tool.status];

  return (
    <div className="my-2 rounded-lg border border-border bg-card/50">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 w-full px-3 py-2 text-xs text-left hover:bg-accent/50 transition-colors rounded-lg"
      >
        <ChevronRight
          className={`w-3 h-3 text-muted-foreground transition-transform flex-shrink-0 ${
            expanded ? 'rotate-90' : ''
          }`}
        />
        <Wrench className="w-3 h-3 text-muted-foreground flex-shrink-0" />
        <span className="font-mono font-medium text-foreground">{tool.name}</span>
        <span className="ml-auto flex-shrink-0">{statusIcon}</span>
      </button>
      {expanded && (
        <div className="px-3 pb-3 space-y-2">
          {tool.input && (
            <div>
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
                Input
              </p>
              <pre className="text-xs bg-background rounded p-2 overflow-x-auto max-h-32 text-muted-foreground">
                {formatJson(tool.input)}
              </pre>
            </div>
          )}
          {tool.result !== undefined && (
            <div>
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
                Result
              </p>
              <pre className="text-xs bg-background rounded p-2 overflow-x-auto max-h-40 text-muted-foreground">
                {tool.result.length > 500
                  ? tool.result.slice(0, 500) + '...'
                  : tool.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function formatJson(input: string): string {
  try {
    return JSON.stringify(JSON.parse(input), null, 2);
  } catch {
    return input;
  }
}

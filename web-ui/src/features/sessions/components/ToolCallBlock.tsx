import { useState } from 'react';
import { ChevronDown, ChevronRight, Wrench, CheckCircle2, Loader2 } from 'lucide-react';
import type { ToolCall } from '@/stores/chatStore';

interface Props {
  toolCall: ToolCall;
}

export function ToolCallBlock({ toolCall }: Props) {
  const [open, setOpen] = useState(false);

  const isRunning = toolCall.status === 'running';

  return (
    <div className="my-1.5 rounded-md border border-border bg-muted/40 text-sm font-mono overflow-hidden">
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted/60 transition-colors"
        onClick={() => setOpen((o) => !o)}
      >
        {isRunning ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-amber-400" />
        ) : (
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
        )}
        <Wrench className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="font-semibold text-foreground">{toolCall.name}</span>
        {isRunning && (
          <span className="ml-auto text-xs text-amber-400">running</span>
        )}
        {open ? (
          <ChevronDown className="ml-auto h-3.5 w-3.5 text-muted-foreground" />
        ) : (
          <ChevronRight className="ml-auto h-3.5 w-3.5 text-muted-foreground" />
        )}
      </button>

      {open && (
        <div className="border-t border-border">
          {toolCall.input && (
            <div className="px-3 py-2">
              <p className="mb-1 text-xs text-muted-foreground">Input</p>
              <pre className="whitespace-pre-wrap break-all text-xs text-foreground/80">
                {(() => {
                  try {
                    return JSON.stringify(JSON.parse(toolCall.input), null, 2);
                  } catch {
                    return toolCall.input;
                  }
                })()}
              </pre>
            </div>
          )}
          {toolCall.result && (
            <div className="border-t border-border bg-muted/20 px-3 py-2">
              <p className="mb-1 text-xs text-muted-foreground">Result</p>
              <pre className="whitespace-pre-wrap break-all text-xs text-foreground/70">
                {toolCall.result.length > 1200
                  ? toolCall.result.slice(0, 1200) + '\n… (truncated)'
                  : toolCall.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

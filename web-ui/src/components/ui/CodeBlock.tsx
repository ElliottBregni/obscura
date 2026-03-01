import * as React from 'react';
import { Check, Copy } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface CodeBlockProps extends React.HTMLAttributes<HTMLDivElement> {
  code: string;
  language?: string;
}

function CodeBlock({ code, language, className, ...props }: CodeBlockProps) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = React.useCallback(async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [code]);

  return (
    <div
      className={cn('relative rounded-lg border bg-card', className)}
      {...props}
    >
      {language && (
        <div className="flex items-center justify-between border-b px-4 py-2">
          <span className="text-xs font-medium text-muted-foreground">
            {language}
          </span>
          <button
            type="button"
            onClick={handleCopy}
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            {copied ? (
              <>
                <Check className="h-3 w-3" />
                Copied
              </>
            ) : (
              <>
                <Copy className="h-3 w-3" />
                Copy
              </>
            )}
          </button>
        </div>
      )}
      {!language && (
        <button
          type="button"
          onClick={handleCopy}
          className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          {copied ? (
            <Check className="h-3 w-3" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
        </button>
      )}
      <pre className="overflow-auto p-4">
        <code className="font-mono text-sm text-foreground">{code}</code>
      </pre>
    </div>
  );
}

export { CodeBlock };

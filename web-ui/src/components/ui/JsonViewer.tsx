import * as React from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface JsonViewerProps extends React.HTMLAttributes<HTMLDivElement> {
  data: unknown;
  collapsed?: boolean;
}

interface JsonNodeProps {
  name?: string;
  value: unknown;
  defaultCollapsed: boolean;
  depth: number;
}

function getType(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function JsonNode({ name, value, defaultCollapsed, depth }: JsonNodeProps) {
  const type = getType(value);
  const isExpandable = type === 'object' || type === 'array';
  const [isCollapsed, setIsCollapsed] = React.useState(
    defaultCollapsed && depth > 0
  );

  if (!isExpandable) {
    return (
      <div className="flex items-baseline gap-1 py-0.5" style={{ paddingLeft: depth * 16 }}>
        {name !== undefined && (
          <span className="text-sky-400">&quot;{name}&quot;</span>
        )}
        {name !== undefined && <span className="text-muted-foreground">: </span>}
        <JsonValue value={value} type={type} />
      </div>
    );
  }

  const entries =
    type === 'array'
      ? (value as unknown[]).map((v, i) => [String(i), v] as const)
      : Object.entries(value as Record<string, unknown>);

  const bracketOpen = type === 'array' ? '[' : '{';
  const bracketClose = type === 'array' ? ']' : '}';

  return (
    <div>
      <button
        type="button"
        className="flex w-full items-baseline gap-1 py-0.5 text-left hover:bg-muted/50"
        style={{ paddingLeft: depth * 16 }}
        onClick={() => setIsCollapsed(!isCollapsed)}
      >
        {isCollapsed ? (
          <ChevronRight className="mt-0.5 h-3 w-3 flex-shrink-0 text-muted-foreground" />
        ) : (
          <ChevronDown className="mt-0.5 h-3 w-3 flex-shrink-0 text-muted-foreground" />
        )}
        {name !== undefined && (
          <span className="text-sky-400">&quot;{name}&quot;</span>
        )}
        {name !== undefined && <span className="text-muted-foreground">: </span>}
        <span className="text-muted-foreground">
          {bracketOpen}
          {isCollapsed && (
            <span className="text-xs text-muted-foreground">
              {' '}
              {entries.length} {entries.length === 1 ? 'item' : 'items'}{' '}
            </span>
          )}
          {isCollapsed && bracketClose}
        </span>
      </button>
      {!isCollapsed && (
        <>
          {entries.map(([key, val]) => (
            <JsonNode
              key={key}
              name={type === 'object' ? key : undefined}
              value={val}
              defaultCollapsed={defaultCollapsed}
              depth={depth + 1}
            />
          ))}
          <div
            className="py-0.5 text-muted-foreground"
            style={{ paddingLeft: depth * 16 }}
          >
            {bracketClose}
          </div>
        </>
      )}
    </div>
  );
}

function JsonValue({ value, type }: { value: unknown; type: string }) {
  switch (type) {
    case 'string':
      return (
        <span className="text-emerald-400">
          &quot;{String(value)}&quot;
        </span>
      );
    case 'number':
      return <span className="text-amber-400">{String(value)}</span>;
    case 'boolean':
      return <span className="text-violet-400">{String(value)}</span>;
    case 'null':
      return <span className="text-red-400">null</span>;
    default:
      return <span className="text-muted-foreground">{String(value)}</span>;
  }
}

function JsonViewer({ data, collapsed = false, className, ...props }: JsonViewerProps) {
  return (
    <div
      className={cn(
        'overflow-auto rounded-md border bg-card p-3 font-mono text-xs',
        className
      )}
      {...props}
    >
      <JsonNode value={data} defaultCollapsed={collapsed} depth={0} />
    </div>
  );
}

export { JsonViewer };

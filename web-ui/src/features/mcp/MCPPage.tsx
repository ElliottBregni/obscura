import { useState } from 'react';
import {
  useMCPTools,
  useMCPResources,
  useMCPPrompts,
} from '@/api/hooks/useMCP';
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { JsonViewer } from '@/components/ui/JsonViewer';
import { Spinner } from '@/components/ui/Spinner';
import { ChevronRight, ChevronDown } from 'lucide-react';

export default function MCPPage() {
  const tools = useMCPTools();
  const resources = useMCPResources();
  const prompts = useMCPPrompts();

  const isLoading = tools.isLoading || resources.isLoading || prompts.isLoading;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">MCP</h1>
        <p className="mt-1 text-muted-foreground">
          Model Context Protocol tools, resources, and prompt templates.
        </p>
      </div>

      {isLoading && (
        <div className="flex items-center justify-center py-12">
          <Spinner size={32} />
        </div>
      )}

      {/* Tools Section */}
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Tools</h2>
        {tools.error && (
          <p className="text-sm text-red-500">
            Failed to load tools: {(tools.error as Error).message}
          </p>
        )}
        {tools.data && tools.data.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No MCP tools registered.
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {tools.data?.map((tool) => (
            <ToolCard
              key={tool.name}
              name={tool.name}
              description={tool.description}
              schema={tool.inputSchema}
            />
          ))}
        </div>
      </section>

      {/* Resources Section */}
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Resources</h2>
        {resources.error && (
          <p className="text-sm text-red-500">
            Failed to load resources: {(resources.error as Error).message}
          </p>
        )}
        {resources.data && resources.data.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No MCP resources registered.
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {resources.data?.map((resource) => (
            <Card key={resource.uri}>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">{resource.name}</CardTitle>
                {resource.description && (
                  <CardDescription>{resource.description}</CardDescription>
                )}
              </CardHeader>
              <CardContent>
                <code className="break-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                  {resource.uri}
                </code>
                {resource.mimeType && (
                  <Badge variant="secondary" className="ml-2">
                    {resource.mimeType}
                  </Badge>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      {/* Prompts Section */}
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Prompts</h2>
        {prompts.error && (
          <p className="text-sm text-red-500">
            Failed to load prompts: {(prompts.error as Error).message}
          </p>
        )}
        {prompts.data && prompts.data.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No MCP prompt templates registered.
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {prompts.data?.map((prompt) => (
            <Card key={prompt.name}>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">{prompt.name}</CardTitle>
                {prompt.description && (
                  <CardDescription>{prompt.description}</CardDescription>
                )}
              </CardHeader>
              {prompt.arguments && prompt.arguments.length > 0 && (
                <CardContent>
                  <p className="mb-1.5 text-xs font-medium text-muted-foreground">
                    Arguments
                  </p>
                  <ul className="space-y-1">
                    {prompt.arguments.map((arg) => (
                      <li
                        key={arg.name}
                        className="flex items-center gap-2 text-sm"
                      >
                        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                          {arg.name}
                        </code>
                        {arg.required && (
                          <Badge
                            variant="outline"
                            className="text-[10px] px-1.5 py-0"
                          >
                            required
                          </Badge>
                        )}
                        {arg.description && (
                          <span className="text-xs text-muted-foreground">
                            {arg.description}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </CardContent>
              )}
            </Card>
          ))}
        </div>
      </section>
    </div>
  );
}

function ToolCard({
  name,
  description,
  schema,
}: {
  name: string;
  description?: string;
  schema?: Record<string, unknown>;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">{name}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      {schema && (
        <CardContent className="space-y-2">
          <button
            type="button"
            className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            Parameters
          </button>
          {expanded && <JsonViewer data={schema} collapsed />}
        </CardContent>
      )}
    </Card>
  );
}

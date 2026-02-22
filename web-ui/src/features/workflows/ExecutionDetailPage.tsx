import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, Clock } from 'lucide-react';
import { useWorkflowExecution } from '@/api/hooks/useWorkflows';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/Card';
import { JsonViewer } from '@/components/ui/JsonViewer';
import { Spinner } from '@/components/ui/Spinner';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { formatDate, formatDuration } from '@/lib/utils';

export default function ExecutionDetailPage() {
  const { executionId } = useParams<{ executionId: string }>();
  const executionQuery = useWorkflowExecution(executionId);

  if (executionQuery.isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (executionQuery.isError || !executionQuery.data) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-destructive">
          Failed to load execution.
        </CardContent>
      </Card>
    );
  }

  const exec = executionQuery.data;

  const duration =
    exec.started_at && exec.completed_at
      ? new Date(exec.completed_at).getTime() -
        new Date(exec.started_at).getTime()
      : null;

  const stepEntries = exec.step_results
    ? Object.entries(exec.step_results)
    : [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" asChild>
          <Link to={`/workflows/${exec.workflow_id}`}>
            <ArrowLeft className="h-4 w-4" />
          </Link>
        </Button>
        <Clock className="h-6 w-6 text-primary" />
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Execution</h1>
          <p className="font-mono text-sm text-muted-foreground">
            {exec.execution_id}
          </p>
        </div>
      </div>

      {/* Summary */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Summary</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">Status</dt>
              <dd className="mt-1">
                <StatusBadge status={exec.status} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Workflow</dt>
              <dd className="mt-1">
                <Link
                  to={`/workflows/${exec.workflow_id}`}
                  className="text-sm font-medium hover:underline"
                >
                  {exec.workflow_id}
                </Link>
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Started</dt>
              <dd className="mt-1 text-sm">
                {exec.started_at ? formatDate(exec.started_at) : '--'}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Duration</dt>
              <dd className="mt-1 text-sm">
                {duration !== null ? formatDuration(duration) : '--'}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* Error */}
      {exec.status === 'failed' && exec.outputs?.error != null && (
        <Card className="border-destructive/50">
          <CardHeader>
            <CardTitle className="text-base text-destructive">Error</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="overflow-auto rounded-md bg-destructive/10 p-4 font-mono text-sm text-destructive">
              {String(exec.outputs.error)}
            </pre>
          </CardContent>
        </Card>
      )}

      {/* Step results */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Step Results</CardTitle>
          <CardDescription>
            {stepEntries.length} step{stepEntries.length !== 1 ? 's' : ''}{' '}
            executed
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {stepEntries.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No step results available.
            </p>
          ) : (
            stepEntries.map(([stepName, result]) => (
              <div key={stepName} className="space-y-2">
                <div className="flex items-center gap-2">
                  <Badge variant="outline">{stepName}</Badge>
                </div>
                <JsonViewer data={result} collapsed />
              </div>
            ))
          )}
        </CardContent>
      </Card>

      {/* Outputs */}
      {exec.outputs && Object.keys(exec.outputs).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Outputs</CardTitle>
          </CardHeader>
          <CardContent>
            <JsonViewer data={exec.outputs} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}

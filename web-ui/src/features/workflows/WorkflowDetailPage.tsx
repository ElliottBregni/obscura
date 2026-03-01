import { useState, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { toast } from 'sonner';
import {
  ArrowLeft,
  GitBranch,
  Play,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import {
  useWorkflow,
  useWorkflowExecutions,
  useExecuteWorkflow,
} from '@/api/hooks/useWorkflows';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/Card';
import { Label } from '@/components/ui/Label';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/Table';
import { JsonViewer } from '@/components/ui/JsonViewer';
import { Spinner } from '@/components/ui/Spinner';
import { StatusBadge } from '@/components/ui/StatusBadge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/Dialog';
import { formatDate } from '@/lib/utils';

function CollapsibleConfig({
  config,
}: {
  config: Record<string, unknown> | undefined;
}) {
  const [open, setOpen] = useState(false);
  if (!config || Object.keys(config).length === 0) {
    return (
      <span className="text-xs text-muted-foreground">No config</span>
    );
  }
  return (
    <div>
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        onClick={() => setOpen(!open)}
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        Config
      </button>
      {open && <JsonViewer data={config} collapsed className="mt-2" />}
    </div>
  );
}

export default function WorkflowDetailPage() {
  const { workflowId } = useParams<{ workflowId: string }>();
  const workflowQuery = useWorkflow(workflowId);
  const executionsQuery = useWorkflowExecutions(workflowId);
  const executeWorkflow = useExecuteWorkflow();

  const [executeOpen, setExecuteOpen] = useState(false);
  const [inputsJson, setInputsJson] = useState('{}');

  const handleExecute = useCallback(() => {
    if (!workflowId) return;
    try {
      const inputs = JSON.parse(inputsJson);
      executeWorkflow.mutate(
        { id: workflowId, inputs },
        {
          onSuccess: (execution) => {
            toast.success(`Execution ${execution.execution_id} started`);
            setExecuteOpen(false);
          },
          onError: (err) => toast.error(`Execution failed: ${String(err)}`),
        },
      );
    } catch {
      toast.error('Invalid JSON inputs');
    }
  }, [executeWorkflow, workflowId, inputsJson]);

  if (workflowQuery.isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (workflowQuery.isError || !workflowQuery.data) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-destructive">
          Failed to load workflow.
        </CardContent>
      </Card>
    );
  }

  const wf = workflowQuery.data;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" asChild>
            <Link to="/workflows">
              <ArrowLeft className="h-4 w-4" />
            </Link>
          </Button>
          <GitBranch className="h-6 w-6 text-primary" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">{wf.name}</h1>
            {wf.description && (
              <p className="text-sm text-muted-foreground">{wf.description}</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Badge variant="outline">
            {wf.steps.length} step{wf.steps.length !== 1 ? 's' : ''}
          </Badge>
          <Button onClick={() => setExecuteOpen(true)}>
            <Play className="mr-1.5 h-4 w-4" />
            Execute
          </Button>
        </div>
      </div>

      {/* Steps */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Steps</CardTitle>
          <CardDescription>
            Workflow steps and their configurations.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!wf.steps?.length ? (
            <p className="py-4 text-sm text-muted-foreground">
              No steps defined.
            </p>
          ) : (
            <div className="space-y-3">
              {wf.steps.map((step, i) => (
                <div
                  key={step.name}
                  className="rounded-lg border bg-muted/20 p-4"
                >
                  <div className="flex items-center gap-3">
                    <Badge variant="outline" className="text-xs">
                      {i + 1}
                    </Badge>
                    <span className="font-medium">{step.name}</span>
                    {step.depends_on?.length ? (
                      <div className="flex items-center gap-1">
                        <span className="text-xs text-muted-foreground">
                          depends on:
                        </span>
                        {step.depends_on.map((dep) => (
                          <Badge
                            key={dep}
                            variant="secondary"
                            className="text-xs"
                          >
                            {dep}
                          </Badge>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  <div className="mt-2">
                    <CollapsibleConfig config={step.agent_config} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Execution history */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Execution History</CardTitle>
        </CardHeader>
        <CardContent>
          {executionsQuery.isLoading ? (
            <div className="flex h-24 items-center justify-center">
              <Spinner size={20} />
            </div>
          ) : executionsQuery.isError ? (
            <p className="text-sm text-destructive">
              Failed to load executions.
            </p>
          ) : !executionsQuery.data?.length ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No executions yet.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Execution ID</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Started</TableHead>
                  <TableHead>Completed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {executionsQuery.data.map((exec) => (
                  <TableRow key={exec.execution_id}>
                    <TableCell>
                      <Link
                        to={`/workflows/executions/${exec.execution_id}`}
                        className="font-mono text-xs hover:underline"
                      >
                        {exec.execution_id}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={exec.status} />
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {exec.started_at ? formatDate(exec.started_at) : '--'}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {exec.completed_at
                        ? formatDate(exec.completed_at)
                        : '--'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Execute dialog */}
      <Dialog open={executeOpen} onOpenChange={setExecuteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Execute Workflow</DialogTitle>
            <DialogDescription>
              Run &quot;{wf.name}&quot; with optional inputs.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="exec-detail-inputs">Inputs (JSON)</Label>
            <textarea
              id="exec-detail-inputs"
              className="h-32 w-full rounded-md border border-input bg-background p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              value={inputsJson}
              onChange={(e) => setInputsJson(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setExecuteOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleExecute} disabled={executeWorkflow.isPending}>
              {executeWorkflow.isPending ? (
                <Spinner size={16} className="mr-2" />
              ) : null}
              Execute
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

import { useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { GitBranch, Plus, Play, Trash2 } from 'lucide-react';
import {
  useWorkflows,
  useExecuteWorkflow,
  useDeleteWorkflow,
} from '@/api/hooks/useWorkflows';
import type { Workflow } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent } from '@/components/ui/Card';
import { Label } from '@/components/ui/Label';
import { Spinner } from '@/components/ui/Spinner';
import { EmptyState } from '@/components/ui/EmptyState';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/Dialog';
import { formatDate } from '@/lib/utils';

export default function WorkflowsPage() {
  const workflowsQuery = useWorkflows();
  const executeWorkflow = useExecuteWorkflow();
  const deleteWorkflow = useDeleteWorkflow();

  const [executeDialogOpen, setExecuteDialogOpen] = useState(false);
  const [targetWorkflow, setTargetWorkflow] = useState<Workflow | null>(null);
  const [inputsJson, setInputsJson] = useState('{}');

  const openExecuteDialog = useCallback((wf: Workflow) => {
    setTargetWorkflow(wf);
    setInputsJson('{}');
    setExecuteDialogOpen(true);
  }, []);

  const handleExecute = useCallback(() => {
    if (!targetWorkflow) return;
    try {
      const inputs = JSON.parse(inputsJson);
      executeWorkflow.mutate(
        { id: targetWorkflow.workflow_id, inputs },
        {
          onSuccess: (execution) => {
            toast.success(`Execution ${execution.execution_id} started`);
            setExecuteDialogOpen(false);
          },
          onError: (err) => toast.error(`Execution failed: ${String(err)}`),
        },
      );
    } catch {
      toast.error('Invalid JSON inputs');
    }
  }, [executeWorkflow, targetWorkflow, inputsJson]);

  const handleDelete = useCallback(
    (wf: Workflow) => {
      if (!confirm(`Delete workflow "${wf.name}"?`)) return;
      deleteWorkflow.mutate(wf.workflow_id, {
        onSuccess: () => toast.success('Workflow deleted'),
        onError: (err) => toast.error(`Delete failed: ${String(err)}`),
      });
    },
    [deleteWorkflow],
  );

  if (workflowsQuery.isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (workflowsQuery.isError) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-destructive">
          Failed to load workflows. Please try again.
        </CardContent>
      </Card>
    );
  }

  const workflows = workflowsQuery.data ?? [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <GitBranch className="h-6 w-6 text-primary" />
          <h1 className="text-2xl font-bold tracking-tight">Workflows</h1>
          <Badge variant="secondary">{workflows.length}</Badge>
        </div>
        <Button asChild>
          <Link to="/workflows/create">
            <Plus className="mr-1.5 h-4 w-4" />
            Create Workflow
          </Link>
        </Button>
      </div>

      {/* Workflow list */}
      {!workflows.length ? (
        <EmptyState
          icon={GitBranch}
          title="No Workflows"
          description="Create your first workflow to orchestrate multi-step agent tasks."
          action={
            <Button asChild>
              <Link to="/workflows/create">
                <Plus className="mr-1.5 h-4 w-4" />
                Create Workflow
              </Link>
            </Button>
          }
        />
      ) : (
        <div className="space-y-3">
          {workflows.map((wf) => (
            <Card key={wf.workflow_id} className="transition-colors hover:bg-muted/30">
              <CardContent className="flex items-center justify-between py-4">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex items-center gap-3">
                    <Link
                      to={`/workflows/${wf.workflow_id}`}
                      className="font-medium hover:underline"
                    >
                      {wf.name}
                    </Link>
                    <Badge variant="outline" className="text-xs">
                      {wf.steps.length} step{wf.steps.length !== 1 ? 's' : ''}
                    </Badge>
                  </div>
                  {wf.description && (
                    <p className="text-sm text-muted-foreground">
                      {wf.description}
                    </p>
                  )}
                  {wf.created_at && (
                    <p className="text-xs text-muted-foreground">
                      Created {formatDate(wf.created_at)}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => openExecuteDialog(wf)}
                  >
                    <Play className="mr-1.5 h-3.5 w-3.5" />
                    Execute
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8"
                    onClick={() => handleDelete(wf)}
                  >
                    <Trash2 className="h-4 w-4 text-destructive" />
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Execute dialog */}
      <Dialog open={executeDialogOpen} onOpenChange={setExecuteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Execute Workflow</DialogTitle>
            <DialogDescription>
              Run &quot;{targetWorkflow?.name}&quot; with optional inputs.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="exec-inputs">Inputs (JSON)</Label>
            <textarea
              id="exec-inputs"
              className="h-32 w-full rounded-md border border-input bg-background p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              value={inputsJson}
              onChange={(e) => setInputsJson(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setExecuteDialogOpen(false)}
            >
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

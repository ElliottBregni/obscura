import { useState } from 'react';
import {
  useWorkflows,
  useWorkflowExecutions,
  useExecuteWorkflow,
  useDeleteWorkflow,
  Workflow,
} from '@/api/client';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/Table';
import {
  Workflow as WorkflowIcon, Plus, Play,
  CheckCircle, XCircle, Trash2, Loader2
} from 'lucide-react';
import { formatDate, cn } from '@/lib/utils';
import { toast } from 'sonner';

function WorkflowCard({ workflow, onExecute, onDelete }: {
  workflow: Workflow;
  onExecute: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <Card hover>
      <CardContent className="p-5">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 rounded-xl bg-accent flex items-center justify-center">
              <WorkflowIcon className="w-6 h-6 text-blue-400" />
            </div>
            <div>
              <h3 className="font-semibold text-foreground">{workflow.name}</h3>
              <p className="text-sm text-muted-foreground">{workflow.description}</p>
            </div>
          </div>
          <Badge
            variant={workflow.status === 'active' ? 'success' : workflow.status === 'paused' ? 'warning' : 'default'}
          >
            {workflow.status}
          </Badge>
        </div>

        <div className="grid grid-cols-2 gap-4 mt-4 pt-4 border-t border-border">
          <div className="text-center">
            <p className="text-2xl font-semibold text-foreground">{workflow.step_count}</p>
            <p className="text-xs text-muted-foreground">Steps</p>
          </div>
          <div className="text-center">
            <p className="text-sm font-medium text-foreground">
              {workflow.created_at ? formatDate(workflow.created_at) : 'N/A'}
            </p>
            <p className="text-xs text-muted-foreground">Created</p>
          </div>
        </div>

        <div className="flex items-center gap-2 mt-4">
          <Button
            variant="secondary"
            size="sm"
            className="flex-1"
            onClick={() => onExecute(workflow.id)}
          >
            <Play className="w-4 h-4 mr-2" /> Run
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => onDelete(workflow.id)}
          >
            <Trash2 className="w-4 h-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function WorkflowsList() {
  const { data: workflows = [], isLoading } = useWorkflows();
  const executeWorkflow = useExecuteWorkflow();
  const deleteWorkflow = useDeleteWorkflow();
  const [filter, setFilter] = useState('all');
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);

  const { data: executions = [] } = useWorkflowExecutions(selectedWorkflowId || '');

  const filteredWorkflows = workflows.filter(w =>
    filter === 'all' || w.status === filter
  );

  const handleExecute = async (id: string) => {
    try {
      await executeWorkflow.mutateAsync({ id });
      setSelectedWorkflowId(id);
      toast.success('Workflow execution started');
    } catch (e: any) {
      toast.error(e.message || 'Failed to execute workflow');
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteWorkflow.mutateAsync(id);
      toast.success('Workflow deleted');
    } catch (e: any) {
      toast.error(e.message || 'Failed to delete workflow');
    }
  };

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Workflows</h1>
          <p className="text-sm text-muted-foreground mt-1">Automate tasks with visual workflows</p>
        </div>
        <Button>
          <Plus className="w-4 h-4 mr-2" /> Create Workflow
        </Button>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-2">
        {['all', 'active', 'paused', 'archived'].map((status) => (
          <button
            key={status}
            onClick={() => setFilter(status)}
            className={cn(
              'px-4 py-2 rounded-lg text-sm font-medium transition-colors',
              filter === status
                ? 'bg-primary text-white'
                : 'text-muted-foreground hover:text-foreground hover:bg-accent'
            )}
          >
            {status.charAt(0).toUpperCase() + status.slice(1)}
          </button>
        ))}
      </div>

      {/* Workflows Grid */}
      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : filteredWorkflows.length === 0 ? (
        <Card>
          <CardContent className="p-12 text-center">
            <WorkflowIcon className="w-12 h-12 mx-auto mb-4 text-muted-foreground" />
            <p className="text-muted-foreground">No workflows found</p>
            <p className="text-sm text-muted-foreground mt-1">
              Create a workflow via the API to get started
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {filteredWorkflows.map((workflow) => (
            <WorkflowCard
              key={workflow.id}
              workflow={workflow}
              onExecute={handleExecute}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}

      {/* Recent Executions */}
      {selectedWorkflowId && executions.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Recent Executions</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Status</TableHead>
                  <TableHead>Workflow</TableHead>
                  <TableHead>Execution ID</TableHead>
                  <TableHead>Started</TableHead>
                  <TableHead className="text-right">Result</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {executions.map((execution) => {
                  const workflow = workflows.find(w => w.id === execution.workflow_id);
                  return (
                    <TableRow key={execution.execution_id}>
                      <TableCell>
                        {execution.status === 'completed' ? (
                          <CheckCircle className="w-5 h-5 text-emerald-400" />
                        ) : execution.status === 'running' ? (
                          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                        ) : (
                          <XCircle className="w-5 h-5 text-red-400" />
                        )}
                      </TableCell>
                      <TableCell className="font-medium text-foreground">
                        {workflow?.name || execution.workflow_id}
                      </TableCell>
                      <TableCell className="text-muted-foreground text-xs font-mono">
                        {execution.execution_id}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {execution.started_at ? formatDate(execution.started_at) : '-'}
                      </TableCell>
                      <TableCell className="text-right">
                        <Badge variant={execution.status === 'completed' ? 'success' : execution.status === 'running' ? 'primary' : 'danger'}>
                          {execution.status}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

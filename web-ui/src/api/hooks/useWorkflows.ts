import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { Workflow, WorkflowExecution } from '@/api/types';

export function useWorkflows() {
  return useQuery({
    queryKey: ['workflows'],
    queryFn: () => fetchApi<Workflow[]>('/api/v1/workflows'),
  });
}

export function useWorkflow(id: string | undefined) {
  return useQuery({
    queryKey: ['workflows', id],
    queryFn: () => fetchApi<Workflow>(`/api/v1/workflows/${id}`),
    enabled: !!id,
  });
}

export function useCreateWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (
      workflow: Omit<Workflow, 'id' | 'workflow_id' | 'status' | 'step_count' | 'created_at'>
    ) =>
      fetchApi<Workflow>('/api/v1/workflows', {
        method: 'POST',
        body: JSON.stringify(workflow),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflows'] });
    },
  });
}

export function useDeleteWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<void>(`/api/v1/workflows/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflows'] });
    },
  });
}

export function useExecuteWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      inputs,
    }: {
      id: string;
      inputs?: Record<string, unknown>;
    }) =>
      fetchApi<WorkflowExecution>(`/api/v1/workflows/${id}/execute`, {
        method: 'POST',
        body: JSON.stringify(inputs ?? {}),
      }),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({
        queryKey: ['workflows', id, 'executions'],
      });
    },
  });
}

export function useWorkflowExecutions(workflowId: string | undefined) {
  return useQuery({
    queryKey: ['workflows', workflowId, 'executions'],
    queryFn: () =>
      fetchApi<WorkflowExecution[]>(
        `/api/v1/workflows/${workflowId}/executions`
      ),
    enabled: !!workflowId,
  });
}

export function useWorkflowExecution(executionId: string | undefined) {
  return useQuery({
    queryKey: ['workflow-executions', executionId],
    queryFn: () =>
      fetchApi<WorkflowExecution>(
        `/api/v1/workflow-executions/${executionId}`
      ),
    enabled: !!executionId,
  });
}

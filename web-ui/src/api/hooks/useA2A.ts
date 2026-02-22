import { useQuery, useMutation } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { A2AAgentCard, A2ATask } from '@/api/types';

interface JSONRPCResponse<T> {
  jsonrpc: '2.0';
  id: number;
  result: T;
}

function a2aRpc(method: string, params?: Record<string, unknown>) {
  return {
    method: 'POST' as const,
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method,
      params: params ?? {},
    }),
  };
}

export function useA2AAgentCard() {
  return useQuery({
    queryKey: ['a2a', 'agent-card'],
    queryFn: () => fetchApi<A2AAgentCard>('/.well-known/agent.json'),
  });
}

export function useA2ATasks() {
  return useQuery({
    queryKey: ['a2a', 'tasks'],
    queryFn: async () => {
      const res = await fetchApi<JSONRPCResponse<{ tasks: A2ATask[] }>>(
        '/a2a/jsonrpc',
        a2aRpc('tasks/list')
      );
      return res.result.tasks;
    },
  });
}

export function useA2ACreateTask() {
  return useMutation({
    mutationFn: (params: {
      message: string;
      skill?: string;
      metadata?: Record<string, unknown>;
    }) =>
      fetchApi<JSONRPCResponse<A2ATask>>(
        '/a2a/jsonrpc',
        a2aRpc('tasks/send', params)
      ).then((res) => res.result),
  });
}

export function useA2ACancelTask() {
  return useMutation({
    mutationFn: (taskId: string) =>
      fetchApi<JSONRPCResponse<{ task_id: string; status: string }>>(
        '/a2a/jsonrpc',
        a2aRpc('tasks/cancel', { task_id: taskId })
      ).then((res) => res.result),
  });
}

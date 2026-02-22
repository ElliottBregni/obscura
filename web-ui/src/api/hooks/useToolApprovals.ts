import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { ToolApproval } from '@/api/types';

export function useToolApprovals(status?: ToolApproval['status']) {
  const params = status ? `?status=${encodeURIComponent(status)}` : '';
  return useQuery({
    queryKey: ['tool-approvals', status],
    queryFn: () =>
      fetchApi<ToolApproval[]>(`/api/v1/tool-approvals${params}`),
  });
}

export function useToolApproval(id: string | undefined) {
  return useQuery({
    queryKey: ['tool-approvals', id],
    queryFn: () => fetchApi<ToolApproval>(`/api/v1/tool-approvals/${id}`),
    enabled: !!id,
  });
}

export function useResolveApproval() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      approved,
      reason,
    }: {
      id: string;
      approved: boolean;
      reason?: string;
    }) =>
      fetchApi<ToolApproval>(`/api/v1/tool-approvals/${id}/resolve`, {
        method: 'POST',
        body: JSON.stringify({ approved, reason }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tool-approvals'] });
    },
  });
}

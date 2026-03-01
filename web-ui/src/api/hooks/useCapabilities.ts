import { useQuery, useMutation } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { CapabilityTier } from '@/api/types';

export function useCapabilityTier() {
  return useQuery({
    queryKey: ['capabilities', 'tier'],
    queryFn: () => fetchApi<CapabilityTier>('/api/v1/capabilities/tier'),
  });
}

export function useGenerateToken() {
  return useMutation({
    mutationFn: (req: { session_id: string }) =>
      fetchApi<{
        tier: string;
        session_id: string;
        expires_at: number;
        token: Record<string, unknown>;
      }>('/api/v1/capabilities/token', {
        method: 'POST',
        body: JSON.stringify(req),
      }),
  });
}

export function useValidateToken() {
  return useMutation({
    mutationFn: (token: {
      tier: string;
      user_id: string;
      session_id: string;
      issued_at: number;
      expires_at: number;
      nonce: string;
      signature: string;
    }) =>
      fetchApi<{ valid: boolean; tier: string; expired: boolean }>(
        '/api/v1/capabilities/validate',
        {
          method: 'POST',
          body: JSON.stringify(token),
        }
      ),
  });
}

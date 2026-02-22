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
    mutationFn: (req: { scope?: string; ttl?: number }) =>
      fetchApi<{ token: string; expires_at: string }>(
        '/api/v1/capabilities/token',
        {
          method: 'POST',
          body: JSON.stringify(req),
        }
      ),
  });
}

export function useValidateToken() {
  return useMutation({
    mutationFn: (token: string) =>
      fetchApi<{ valid: boolean; tier?: string; roles?: string[] }>(
        '/api/v1/capabilities/validate',
        {
          method: 'POST',
          body: JSON.stringify({ token }),
        }
      ),
  });
}

import { useQuery, useMutation } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';
import type { Skill } from '@/api/types';

export function useSkills() {
  return useQuery({
    queryKey: ['skills'],
    queryFn: async () => {
      const data = await fetchApi<{ skills: Skill[]; count: number }>(
        '/api/v1/skills'
      );
      return data.skills;
    },
  });
}

export function useSkill(name: string | undefined) {
  return useQuery({
    queryKey: ['skills', name],
    queryFn: () => fetchApi<Skill>(`/api/v1/skills/${encodeURIComponent(name!)}`),
    enabled: !!name,
  });
}

export function useExecuteSkill() {
  return useMutation({
    mutationFn: ({
      name,
      inputs,
    }: {
      name: string;
      inputs?: Record<string, unknown>;
    }) =>
      fetchApi<{ result: unknown }>(
        `/api/v1/skills/${encodeURIComponent(name)}/execute`,
        {
          method: 'POST',
          body: JSON.stringify(inputs ?? {}),
        }
      ),
  });
}

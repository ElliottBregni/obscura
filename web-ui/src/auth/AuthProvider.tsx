import { type ReactNode, useEffect } from 'react';
import { useAuthStore } from '@/stores/authStore';
import { useSystemStore } from '@/stores/systemStore';
import { API_URL } from '@/lib/constants';

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const token = useAuthStore((s) => s.token);
  const setAuthEnabled = useSystemStore((s) => s.setAuthEnabled);
  const setServerReachable = useSystemStore((s) => s.setServerReachable);

  // Check server health on mount to determine if auth is enabled
  useEffect(() => {
    async function checkHealth() {
      try {
        const resp = await fetch(`${API_URL}/health`);
        if (resp.ok) {
          const data = await resp.json();
          setAuthEnabled(data.auth_enabled ?? false);
          setServerReachable(true);
        } else if (resp.status === 401) {
          setAuthEnabled(true);
          setServerReachable(true);
        }
      } catch {
        setServerReachable(false);
      }
    }

    checkHealth();
  }, [token, setAuthEnabled, setServerReachable]);

  return <>{children}</>;
}

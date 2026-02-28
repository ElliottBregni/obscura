import { type ReactNode, useEffect } from 'react';
import { useAuthStore } from '@/stores/authStore';
import { useSystemStore } from '@/stores/systemStore';
import { API_URL } from '@/lib/constants';

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const token = useAuthStore((s) => s.token);
  const apiKey = useAuthStore((s) => s.apiKey);
  const setApiKey = useAuthStore((s) => s.setApiKey);
  const setAuthEnabled = useSystemStore((s) => s.setAuthEnabled);
  const setServerReachable = useSystemStore((s) => s.setServerReachable);
  const devApiKey = import.meta.env.VITE_DEV_API_KEY as string | undefined;
  const forceDevApiKey = import.meta.env.VITE_FORCE_DEV_API_KEY === 'true';

  // Check server health on mount to determine if auth is enabled
  useEffect(() => {
    async function checkHealth() {
      if (forceDevApiKey && devApiKey && apiKey !== devApiKey) {
        setApiKey(devApiKey);
      }
      const healthEndpoint = API_URL ? `${API_URL}/api/v1/health` : '/api/v1/health';
      const headers: Record<string, string> = {};
      if (forceDevApiKey && devApiKey) {
        headers['X-API-Key'] = devApiKey;
      } else if (apiKey) {
        headers['X-API-Key'] = apiKey;
      } else if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }
      try {
        const resp = await fetch(healthEndpoint, { headers });
        if (resp.ok) {
          setAuthEnabled(true);
          setServerReachable(true);
          if (!token && !apiKey && devApiKey) {
            setApiKey(devApiKey);
          }
        } else if (resp.status === 401) {
          setAuthEnabled(true);
          setServerReachable(true);
          if (!token && !apiKey && devApiKey) {
            setApiKey(devApiKey);
          }
        } else {
          setAuthEnabled(true);
          setServerReachable(true);
        }
      } catch {
        setAuthEnabled(true);
        setServerReachable(false);
      }
    }

    checkHealth();
  }, [
    apiKey,
    devApiKey,
    forceDevApiKey,
    setApiKey,
    setAuthEnabled,
    setServerReachable,
    token,
  ]);

  return <>{children}</>;
}

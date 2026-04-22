import { type ReactNode, useEffect } from 'react';
import { useAuthStore } from '@/stores/authStore';
import { useSystemStore } from '@/stores/systemStore';
import { API_URL } from '@/lib/constants';
import { supabase } from '@/lib/supabase';

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const token = useAuthStore((s) => s.token);
  const apiKey = useAuthStore((s) => s.apiKey);
  const setToken = useAuthStore((s) => s.setToken);
  const setApiKey = useAuthStore((s) => s.setApiKey);
  const logout = useAuthStore((s) => s.logout);
  const setAuthEnabled = useSystemStore((s) => s.setAuthEnabled);
  const setServerReachable = useSystemStore((s) => s.setServerReachable);
  const devApiKey = import.meta.env.VITE_DEV_API_KEY as string | undefined;
  const forceDevApiKey = import.meta.env.VITE_FORCE_DEV_API_KEY === 'true';

  // ── Supabase session → authStore ──────────────────────────────────────
  // Bridge Supabase sessions into the existing token-based store so the rest
  // of the app (API client, route guards, role checks) can stay unchanged.
  useEffect(() => {
    if (!supabase) return;

    let cancelled = false;

    // Hydrate from any existing session (e.g. after a page refresh or a
    // redirect-back from an OAuth provider).
    void supabase.auth.getSession().then(({ data }) => {
      if (cancelled) return;
      if (data.session?.access_token) {
        setToken(data.session.access_token);
      }
    });

    const { data: sub } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === 'SIGNED_OUT' || !session) {
        // Only blow away the store if the user wasn't on an API-key bypass.
        if (!useAuthStore.getState().apiKey) {
          logout();
        }
        return;
      }
      setToken(session.access_token);
    });

    return () => {
      cancelled = true;
      sub.subscription.unsubscribe();
    };
  }, [setToken, logout]);

  // ── Server health probe ───────────────────────────────────────────────
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

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
  const setGithubToken = useAuthStore((s) => s.setGithubToken);
  const logout = useAuthStore((s) => s.logout);
  const setAuthEnabled = useSystemStore((s) => s.setAuthEnabled);
  const setServerReachable = useSystemStore((s) => s.setServerReachable);
  const devApiKey = import.meta.env.VITE_DEV_API_KEY as string | undefined;
  // Security: refuse the dev-key bypass in production builds even if
  // VITE_FORCE_DEV_API_KEY=true accidentally leaks into the build env.
  const isProdBuild = import.meta.env.PROD === true;
  const forceDevApiKey =
    !isProdBuild && import.meta.env.VITE_FORCE_DEV_API_KEY === 'true';
  if (isProdBuild && import.meta.env.VITE_FORCE_DEV_API_KEY === 'true') {
    // eslint-disable-next-line no-console
    console.error(
      '[Obscura] VITE_FORCE_DEV_API_KEY is set in a production build — ignored. ' +
        'Remove it from your build env.',
    );
  }

  // ── Supabase session → authStore ──────────────────────────────────────
  useEffect(() => {
    if (!supabase) return;

    let cancelled = false;

    void supabase.auth.getSession().then(({ data }) => {
      if (cancelled) return;
      if (data.session?.access_token) {
        setToken(data.session.access_token);
      }
      // `provider_token` only populated on GitHub (and other third-party
      // OAuth) sign-ins. Used as the "easy path" Copilot fallback.
      if (data.session?.provider_token) {
        setGithubToken(data.session.provider_token);
      }
    });

    const { data: sub } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === 'SIGNED_OUT' || !session) {
        if (!useAuthStore.getState().apiKey) {
          logout();
        }
        return;
      }
      setToken(session.access_token);
      if (session.provider_token) {
        setGithubToken(session.provider_token);
      }
    });

    return () => {
      cancelled = true;
      sub.subscription.unsubscribe();
    };
  }, [setToken, setGithubToken, logout]);

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
        if (resp.ok || resp.status === 401) {
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

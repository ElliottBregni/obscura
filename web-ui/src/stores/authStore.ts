import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { decodeJWT, extractUser, isTokenExpired } from '@/lib/jwt';
import type { DecodedUser } from '@/lib/jwt';
import { supabase } from '@/lib/supabase';

interface AuthState {
  token: string | null;
  apiKey: string | null;
  // Supabase-forwarded GitHub OAuth token — captured from
  // `session.provider_token` after a GitHub sign-in. Sent as `X-GitHub-Token`
  // on API requests so the server can feed it to Copilot as the "easy path"
  // fallback (see obscura.core.auth.AuthConfig.oauth_github_token).
  githubToken: string | null;
  user: DecodedUser | null;
  isAuthenticated: boolean;

  setToken: (token: string) => void;
  setApiKey: (key: string) => void;
  setGithubToken: (token: string | null) => void;
  logout: () => void;
  hasRole: (role: string) => boolean;
  hasAnyRole: (...roles: string[]) => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      apiKey: null,
      githubToken: null,
      user: null,
      isAuthenticated: false,

      setToken: (token: string) => {
        try {
          const payload = decodeJWT(token);
          if (isTokenExpired(payload)) {
            set({ token: null, user: null, isAuthenticated: false });
            return;
          }
          const user = extractUser(payload);
          set({ token, user, isAuthenticated: true, apiKey: null });
        } catch {
          set({ token: null, user: null, isAuthenticated: false });
        }
      },

      setApiKey: (apiKey: string) => {
        set({
          apiKey,
          token: null,
          user: { userId: 'api-key-user', email: '', roles: ['admin'], orgId: null },
          isAuthenticated: true,
        });
      },

      setGithubToken: (githubToken: string | null) => {
        set({ githubToken });
      },

      logout: () => {
        set({
          token: null,
          apiKey: null,
          githubToken: null,
          user: null,
          isAuthenticated: false,
        });
        // Fire-and-forget Supabase sign-out so a browser refresh doesn't
        // silently re-authenticate the user via a cached refresh token.
        if (supabase) {
          void supabase.auth.signOut().catch(() => undefined);
        }
      },

      hasRole: (role: string) => {
        const { user } = get();
        if (!user) return false;
        return user.roles.includes('admin') || user.roles.includes(role);
      },

      hasAnyRole: (...roles: string[]) => {
        const { user } = get();
        if (!user) return false;
        if (user.roles.includes('admin')) return true;
        return roles.some((r) => user.roles.includes(r));
      },
    }),
    { name: 'obscura-auth' }
  )
);

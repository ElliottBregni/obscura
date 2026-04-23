import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import { toast } from 'sonner';
import { useAuth } from './useAuth';
import { supabase, supabaseEnabled } from '@/lib/supabase';

type LoginMode = 'oauth' | 'magiclink' | 'apikey';

export function RouteGuard() {
  const { isAuthenticated, setApiKey } = useAuth();
  const [mode, setMode] = useState<LoginMode>(supabaseEnabled ? 'oauth' : 'apikey');
  const [email, setEmail] = useState('');
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [submitting, setSubmitting] = useState(false);

  if (isAuthenticated) return <Outlet />;

  const handleOAuth = async (provider: 'github' | 'google') => {
    if (!supabase) return;
    setSubmitting(true);
    try {
      // Request scopes that let us reuse the GitHub token for Copilot's
      // "easy path" fallback. Copilot itself may still reject the token if
      // the Supabase OAuth app isn't in GitHub's allowlist — the server
      // falls back to env/CLI sources in that case.
      const scopes = provider === 'github' ? 'read:user user:email' : undefined;
      const { error } = await supabase.auth.signInWithOAuth({
        provider,
        options: {
          redirectTo: window.location.origin,
          ...(scopes ? { scopes } : {}),
        },
      });
      if (error) toast.error(`Sign-in failed: ${error.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  const handleMagicLink = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!supabase || !email.trim()) return;
    setSubmitting(true);
    try {
      const { error } = await supabase.auth.signInWithOtp({
        email: email.trim(),
        options: { emailRedirectTo: window.location.origin },
      });
      if (error) {
        toast.error(`Couldn't send link: ${error.message}`);
      } else {
        toast.success(`Magic link sent to ${email.trim()}`);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleApiKey = (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKeyInput.trim()) return;
    setApiKey(apiKeyInput.trim());
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-md space-y-6 rounded-lg border border-border bg-card p-8">
        <div className="space-y-2 text-center">
          <h1 className="text-2xl font-bold tracking-tight">Obscura</h1>
          <p className="text-sm text-muted-foreground">
            Sign in to the admin portal
          </p>
        </div>

        <div className="flex gap-1 rounded-md bg-secondary p-1 text-xs">
          {supabaseEnabled && (
            <>
              <TabButton active={mode === 'oauth'} onClick={() => setMode('oauth')}>
                OAuth
              </TabButton>
              <TabButton
                active={mode === 'magiclink'}
                onClick={() => setMode('magiclink')}
              >
                Magic Link
              </TabButton>
            </>
          )}
          <TabButton active={mode === 'apikey'} onClick={() => setMode('apikey')}>
            API Key
          </TabButton>
        </div>

        {mode === 'oauth' && supabaseEnabled && (
          <div className="space-y-3">
            <button
              onClick={() => handleOAuth('github')}
              disabled={submitting}
              className="w-full rounded-md border border-border bg-background px-4 py-2 text-sm font-medium text-foreground hover:bg-accent transition-colors disabled:opacity-50"
            >
              Continue with GitHub
            </button>
            <button
              onClick={() => handleOAuth('google')}
              disabled={submitting}
              className="w-full rounded-md border border-border bg-background px-4 py-2 text-sm font-medium text-foreground hover:bg-accent transition-colors disabled:opacity-50"
            >
              Continue with Google
            </button>
          </div>
        )}

        {mode === 'magiclink' && supabaseEnabled && (
          <form onSubmit={handleMagicLink} className="space-y-4">
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              required
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
            <button
              type="submit"
              disabled={submitting}
              className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
            >
              Send magic link
            </button>
          </form>
        )}

        {mode === 'apikey' && (
          <form onSubmit={handleApiKey} className="space-y-4">
            <input
              type="password"
              value={apiKeyInput}
              onChange={(e) => setApiKeyInput(e.target.value)}
              placeholder="Enter API key..."
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
            <button
              type="submit"
              className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
            >
              Sign in with API key
            </button>
            {!supabaseEnabled && (
              <p className="text-xs text-muted-foreground text-center">
                Supabase is not configured — set <code>VITE_SUPABASE_URL</code> and{' '}
                <code>VITE_SUPABASE_ANON_KEY</code> to enable OAuth.
              </p>
            )}
          </form>
        )}
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex-1 rounded px-2 py-1.5 font-medium transition-colors ${
        active
          ? 'bg-background text-foreground shadow-sm'
          : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      {children}
    </button>
  );
}

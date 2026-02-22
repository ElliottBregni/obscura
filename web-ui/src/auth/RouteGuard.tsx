import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import { useAuth } from './useAuth';
import { useSystemStore } from '@/stores/systemStore';

export function RouteGuard() {
  const { isAuthenticated, setToken, setApiKey } = useAuth();
  const authEnabled = useSystemStore((s) => s.authEnabled);
  const [keyInput, setKeyInput] = useState('');
  const [mode, setMode] = useState<'apikey' | 'jwt'>('apikey');

  // If auth is disabled on server, allow all access
  if (!authEnabled) return <Outlet />;

  if (isAuthenticated) return <Outlet />;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!keyInput.trim()) return;

    if (mode === 'jwt') {
      setToken(keyInput.trim());
    } else {
      setApiKey(keyInput.trim());
    }
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

        <div className="flex gap-2 rounded-md bg-secondary p-1">
          <button
            onClick={() => setMode('apikey')}
            className={`flex-1 rounded px-3 py-1.5 text-sm font-medium transition-colors ${
              mode === 'apikey'
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            API Key
          </button>
          <button
            onClick={() => setMode('jwt')}
            className={`flex-1 rounded px-3 py-1.5 text-sm font-medium transition-colors ${
              mode === 'jwt'
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            JWT Token
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <input
            type="password"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder={mode === 'apikey' ? 'Enter API key...' : 'Paste JWT token...'}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <button
            type="submit"
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
          >
            Sign In
          </button>
        </form>
      </div>
    </div>
  );
}

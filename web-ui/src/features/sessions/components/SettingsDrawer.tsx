import { useState, useEffect } from 'react';
import { X, Sun, Moon, Key, Copy, Check } from 'lucide-react';
import { useAuthStore } from '@/stores/authStore';

interface Props {
  open: boolean;
  onClose: () => void;
}

function useTheme() {
  const [theme, setThemeState] = useState<'dark' | 'light'>(() => {
    return (localStorage.getItem('obscura:theme') as 'dark' | 'light') ?? 'dark';
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'light') {
      root.classList.remove('dark');
      root.classList.add('light');
    } else {
      root.classList.remove('light');
      root.classList.add('dark');
    }
    localStorage.setItem('obscura:theme', theme);
  }, [theme]);

  return { theme, toggle: () => setThemeState((t) => t === 'dark' ? 'light' : 'dark') };
}

export function SettingsDrawer({ open, onClose }: Props) {
  const { theme, toggle } = useTheme();
  const apiKey = useAuthStore((s) => s.apiKey);
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const [copied, setCopied] = useState(false);

  const maskedKey = apiKey
    ? apiKey.slice(0, 8) + '…' + apiKey.slice(-4)
    : token
    ? 'JWT (via Supabase)'
    : 'None';

  const handleCopy = () => {
    const val = apiKey ?? token ?? '';
    if (!val) return;
    navigator.clipboard.writeText(val);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />

      {/* Drawer — slides in from right */}
      <div className="relative ml-auto flex h-full w-80 flex-col border-l border-border bg-card shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Settings</h2>
          <button onClick={onClose} className="rounded p-1 hover:bg-muted transition-colors">
            <X className="h-4 w-4 text-muted-foreground" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-6">
          {/* Appearance */}
          <section>
            <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Appearance
            </h3>
            <div className="flex items-center justify-between rounded-lg border border-border bg-background px-3 py-2.5">
              <div className="flex items-center gap-2">
                {theme === 'dark' ? (
                  <Moon className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <Sun className="h-4 w-4 text-amber-400" />
                )}
                <span className="text-sm">
                  {theme === 'dark' ? 'Dark mode' : 'Light mode'}
                </span>
              </div>
              <button
                onClick={toggle}
                className={`relative h-5 w-9 rounded-full transition-colors ${
                  theme === 'light' ? 'bg-primary' : 'bg-muted-foreground/30'
                }`}
              >
                <span className={`absolute top-0.5 left-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform ${
                  theme === 'light' ? 'translate-x-4' : 'translate-x-0'
                }`} />
              </button>
            </div>
          </section>

          {/* Auth */}
          <section>
            <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Authentication
            </h3>
            <div className="space-y-2">
              {user && (
                <div className="rounded-lg border border-border bg-background px-3 py-2.5">
                  <p className="text-[10px] text-muted-foreground mb-0.5">Signed in as</p>
                  <p className="text-sm font-medium truncate">{user.email ?? user.userId}</p>
                  {user.roles.length > 0 && (
                    <p className="text-[10px] text-muted-foreground mt-0.5">
                      {user.roles.slice(0, 4).join(', ')}
                      {user.roles.length > 4 && ` +${user.roles.length - 4}`}
                    </p>
                  )}
                </div>
              )}

              <div className="rounded-lg border border-border bg-background px-3 py-2.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2 min-w-0">
                    <Key className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    <div className="min-w-0">
                      <p className="text-[10px] text-muted-foreground">API credential</p>
                      <p className="text-xs font-mono text-foreground truncate">{maskedKey}</p>
                    </div>
                  </div>
                  {(apiKey || token) && (
                    <button
                      onClick={handleCopy}
                      className="ml-2 shrink-0 rounded p-1 hover:bg-muted transition-colors"
                      title="Copy credential"
                    >
                      {copied
                        ? <Check className="h-3.5 w-3.5 text-emerald-500" />
                        : <Copy className="h-3.5 w-3.5 text-muted-foreground" />
                      }
                    </button>
                  )}
                </div>
              </div>
            </div>
          </section>

          {/* Keyboard shortcuts */}
          <section>
            <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Keyboard shortcuts
            </h3>
            <div className="space-y-1.5 text-sm">
              {[
                { keys: ['⌘', 'K'], label: 'Switch session' },
                { keys: ['Esc'], label: 'Cancel stream / close' },
              ].map(({ keys, label }) => (
                <div key={label} className="flex items-center justify-between">
                  <span className="text-muted-foreground text-xs">{label}</span>
                  <div className="flex gap-1">
                    {keys.map((k) => (
                      <kbd key={k} className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground font-mono">
                        {k}
                      </kbd>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>

        <div className="border-t border-border px-4 py-2">
          <p className="text-[10px] text-muted-foreground">Obscura Web UI</p>
        </div>
      </div>
    </div>
  );
}

import { useLocation } from 'react-router-dom';
import { LogOut, Wifi, WifiOff } from 'lucide-react';
import { useAuth } from '@/auth/useAuth';
import { useSystemStore } from '@/stores/systemStore';
import { cn } from '@/lib/utils';

const ROUTE_LABELS: Record<string, string> = {
  '/': 'Dashboard',
  '/agents': 'Agents',
  '/agents/spawn': 'Spawn Agent',
  '/agents/templates': 'Agent Templates',
  '/agents/groups': 'Agent Groups',
  '/memory': 'Memory',
  '/workflows': 'Workflows',
  '/workflows/create': 'Create Workflow',
  '/approvals': 'Tool Approvals',
  '/webhooks': 'Webhooks',
  '/webhooks/create': 'Create Webhook',
  '/audit': 'Audit Logs',
  '/sessions': 'Sessions',
  '/admin': 'Admin',
  '/admin/rate-limits': 'Rate Limits',
  '/admin/capabilities': 'Capabilities',
  '/admin/metrics': 'Metrics',
  '/health': 'Health',
  '/mcp': 'MCP',
  '/a2a': 'A2A',
};

export function Header() {
  const location = useLocation();
  const { user, logout } = useAuth();
  const wsConnected = useSystemStore((s) => s.wsConnected);
  const serverReachable = useSystemStore((s) => s.serverReachable);

  const pageTitle = ROUTE_LABELS[location.pathname] || 'Obscura';

  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-card px-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">{pageTitle}</h1>
      </div>

      <div className="flex items-center gap-4">
        {/* Connection status */}
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {serverReachable ? (
            wsConnected ? (
              <>
                <Wifi className="h-3.5 w-3.5 text-emerald-500" />
                <span>Connected</span>
              </>
            ) : (
              <>
                <Wifi className="h-3.5 w-3.5 text-yellow-500" />
                <span>API only</span>
              </>
            )
          ) : (
            <>
              <WifiOff className="h-3.5 w-3.5 text-red-500" />
              <span>Disconnected</span>
            </>
          )}
        </div>

        {/* User info */}
        {user && (
          <div className="flex items-center gap-3">
            <div className="text-right">
              <div className="text-sm font-medium">{user.email || user.userId}</div>
              <div className="text-[10px] text-muted-foreground">
                {user.roles.slice(0, 3).join(', ')}
                {user.roles.length > 3 && ` +${user.roles.length - 3}`}
              </div>
            </div>
            <button
              onClick={logout}
              className={cn(
                'rounded-md p-2 text-muted-foreground transition-colors',
                'hover:bg-secondary hover:text-foreground'
              )}
              title="Sign out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>
    </header>
  );
}

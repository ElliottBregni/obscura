import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  Bot,
  Brain,
  Target,
  Workflow,
  ShieldCheck,
  Webhook,
  FileSearch,
  MessageSquare,
  Activity,
  Plug,
  Network,
  Settings,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAuth } from '@/auth/useAuth';
import { canAccessSection } from '@/auth/permissions';
import { useUIStore } from '@/stores/uiStore';

interface NavItem {
  label: string;
  path: string;
  icon: React.ElementType;
  section: string;
}

const NAV_ITEMS: (NavItem | 'separator')[] = [
  { label: 'Dashboard',  path: '/',          icon: LayoutDashboard, section: 'dashboard' },
  { label: 'Agents',     path: '/agents',     icon: Bot,             section: 'agents' },
  { label: 'Sessions',   path: '/sessions',   icon: MessageSquare,   section: 'sessions' },
  { label: 'Memory',     path: '/memory',     icon: Brain,           section: 'memory' },
  { label: 'Workflows',  path: '/workflows',  icon: Workflow,        section: 'workflows' },
  { label: 'Goals',      path: '/goals',      icon: Target,          section: 'goals' },
  'separator',
  { label: 'Approvals',  path: '/approvals',  icon: ShieldCheck,     section: 'approvals' },
  { label: 'Webhooks',   path: '/webhooks',   icon: Webhook,         section: 'webhooks' },
  { label: 'Audit',      path: '/audit',      icon: FileSearch,      section: 'audit' },
  'separator',
  { label: 'Health',     path: '/health',     icon: Activity,        section: 'health' },
  { label: 'MCP',        path: '/mcp',        icon: Plug,            section: 'mcp' },
  { label: 'A2A',        path: '/a2a',        icon: Network,         section: 'a2a' },
  'separator',
  { label: 'Admin',      path: '/admin',      icon: Settings,        section: 'admin' },
];

export function Sidebar() {
  const { roles } = useAuth();
  const collapsed = useUIStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);

  return (
    <aside
      className={cn(
        'relative flex h-screen flex-col border-r transition-all duration-200 ease-in-out',
        collapsed ? 'w-[52px]' : 'w-[200px]',
      )}
      style={{
        background: 'hsl(var(--card))',
        borderColor: 'hsl(var(--border))',
      }}
    >
      {/* Wordmark / logo area */}
      <div
        className="flex h-12 items-center overflow-hidden border-b px-3"
        style={{ borderColor: 'hsl(var(--border))' }}
      >
        {collapsed ? (
          /* Compact logo mark */
          <span className="text-gradient text-base font-bold select-none">O</span>
        ) : (
          <span className="text-gradient text-base font-semibold tracking-tight select-none whitespace-nowrap">
            Obscura
          </span>
        )}
      </div>

      {/* Nav items */}
      <nav className="flex-1 overflow-y-auto py-3 scrollbar-hide">
        <div className={cn('flex flex-col gap-0.5', collapsed ? 'px-1.5' : 'px-2')}>
          {NAV_ITEMS.map((item, i) => {
            if (item === 'separator') {
              return (
                <div
                  key={`sep-${i}`}
                  className="my-2 h-px mx-1"
                  style={{ background: 'hsl(var(--border))' }}
                />
              );
            }

            if (!canAccessSection(item.section, roles)) return null;

            return (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.path === '/'}
                title={collapsed ? item.label : undefined}
                className={({ isActive }) =>
                  cn(
                    'group flex items-center gap-2.5 rounded-lg py-2 text-sm font-medium transition-all duration-150',
                    collapsed ? 'justify-center px-0' : 'px-3',
                    isActive
                      ? 'text-primary bg-primary/10'
                      : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <item.icon
                      className={cn(
                        'h-4 w-4 shrink-0 transition-colors',
                        isActive ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground',
                      )}
                    />
                    {!collapsed && (
                      <span className="truncate">{item.label}</span>
                    )}
                  </>
                )}
              </NavLink>
            );
          })}
        </div>
      </nav>

      {/* Collapse toggle */}
      <div
        className="flex h-10 items-center border-t"
        style={{ borderColor: 'hsl(var(--border))' }}
      >
        <button
          type="button"
          onClick={toggleSidebar}
          className={cn(
            'flex w-full items-center text-xs text-muted-foreground',
            'hover:text-foreground transition-colors duration-150',
            collapsed ? 'justify-center py-2' : 'gap-2 px-3 py-2',
          )}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? (
            <ChevronRight className="h-3.5 w-3.5" />
          ) : (
            <>
              <ChevronLeft className="h-3.5 w-3.5" />
              <span>Collapse</span>
            </>
          )}
        </button>
      </div>
    </aside>
  );
}

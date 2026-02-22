import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  Bot,
  Brain,
  Workflow,
  ShieldCheck,
  Webhook,
  FileSearch,
  MessageSquare,
  Activity,
  Plug,
  Network,
  Settings,
  PanelLeftClose,
  PanelLeft,
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
  badge?: number;
}

const NAV_ITEMS: (NavItem | 'separator')[] = [
  { label: 'Dashboard', path: '/', icon: LayoutDashboard, section: 'dashboard' },
  { label: 'Agents', path: '/agents', icon: Bot, section: 'agents' },
  { label: 'Memory', path: '/memory', icon: Brain, section: 'memory' },
  { label: 'Workflows', path: '/workflows', icon: Workflow, section: 'workflows' },
  { label: 'Approvals', path: '/approvals', icon: ShieldCheck, section: 'approvals' },
  { label: 'Webhooks', path: '/webhooks', icon: Webhook, section: 'webhooks' },
  { label: 'Audit', path: '/audit', icon: FileSearch, section: 'audit' },
  { label: 'Sessions', path: '/sessions', icon: MessageSquare, section: 'sessions' },
  'separator',
  { label: 'Health', path: '/health', icon: Activity, section: 'health' },
  { label: 'MCP', path: '/mcp', icon: Plug, section: 'mcp' },
  { label: 'A2A', path: '/a2a', icon: Network, section: 'a2a' },
  'separator',
  { label: 'Admin', path: '/admin', icon: Settings, section: 'admin' },
];

export function Sidebar() {
  const { roles } = useAuth();
  const collapsed = useUIStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);

  return (
    <aside
      className={cn(
        'flex h-screen flex-col border-r border-border bg-card transition-all duration-200',
        collapsed ? 'w-16' : 'w-56'
      )}
    >
      {/* Logo */}
      <div className="flex h-14 items-center justify-between border-b border-border px-4">
        {!collapsed && (
          <span className="text-lg font-semibold tracking-tight">Obscura</span>
        )}
        <button
          onClick={toggleSidebar}
          className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
        >
          {collapsed ? <PanelLeft className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-2 px-2 scrollbar-hide">
        {NAV_ITEMS.map((item, i) => {
          if (item === 'separator') {
            return <div key={`sep-${i}`} className="my-2 h-px bg-border" />;
          }

          if (!canAccessSection(item.section, roles)) return null;

          return (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.path === '/'}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                  collapsed && 'justify-center px-2'
                )
              }
            >
              <item.icon className="h-4 w-4 shrink-0" />
              {!collapsed && (
                <span className="truncate">{item.label}</span>
              )}
              {!collapsed && item.badge !== undefined && item.badge > 0 && (
                <span className="ml-auto flex h-5 min-w-5 items-center justify-center rounded-full bg-primary px-1.5 text-[10px] font-medium text-primary-foreground">
                  {item.badge}
                </span>
              )}
            </NavLink>
          );
        })}
      </nav>
    </aside>
  );
}

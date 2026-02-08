import { useState } from 'react';
import { Link, useLocation, Outlet } from 'react-router-dom';
import { cn } from '@/lib/utils';
import {
  LayoutDashboard,
  Bot,
  Brain,
  Workflow,
  Activity,
  Settings,
  Terminal,
  Puzzle,
  Menu,
  X
} from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Separator } from '@/components/ui/Separator';
import { useAgentStore } from '@/stores/agentStore';
import { useWebSocket } from '@/hooks/useWebSocket';

const navItems = [
  { path: '/', label: 'Dashboard', icon: LayoutDashboard },
  { path: '/agents', label: 'Agents', icon: Bot },
  { path: '/memory', label: 'Memory', icon: Brain },
  { path: '/workflows', label: 'Workflows', icon: Workflow },
  { path: '/skills', label: 'Skills', icon: Puzzle },
  { path: '/health', label: 'Health', icon: Activity },
];

const bottomNavItems = [
  { path: '/settings', label: 'Settings', icon: Settings },
];

function Sidebar({ mobile = false, onClose }: { mobile?: boolean; onClose?: () => void }) {
  const location = useLocation();
  const { agents } = useAgentStore();
  const activeCount = agents.filter(a => a.status === 'running').length;

  return (
    <aside className={cn(
      'flex flex-col border-r border-border bg-background',
      mobile ? 'h-full w-60' : 'w-60 h-screen sticky top-0'
    )}>
      {/* Logo */}
      <div className="h-12 flex items-center px-4 gap-3">
        <div className="w-7 h-7 rounded-md bg-primary flex items-center justify-center">
          <Terminal className="w-4 h-4 text-primary-foreground" />
        </div>
        <span className="font-semibold text-foreground">Obscura</span>
        {mobile && (
          <Button variant="ghost" size="icon" className="ml-auto h-7 w-7" onClick={onClose}>
            <X className="w-4 h-4" />
          </Button>
        )}
      </div>

      <Separator />

      {/* Main Navigation */}
      <nav className="flex-1 py-2 px-2 space-y-0.5 overflow-y-auto">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = location.pathname === item.path ||
            (item.path !== '/' && location.pathname.startsWith(`${item.path}/`));

          return (
            <Link
              key={item.path}
              to={item.path}
              onClick={onClose}
              className={cn(
                'flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                isActive
                  ? 'bg-accent text-foreground'
                  : 'text-muted-foreground hover:text-foreground hover:bg-accent/50'
              )}
            >
              <Icon className="w-4 h-4" />
              {item.label}
              {item.path === '/agents' && activeCount > 0 && (
                <span className="ml-auto text-xs text-emerald-400 bg-emerald-500/10 px-1.5 py-0.5 rounded">
                  {activeCount}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Bottom Navigation */}
      <Separator />
      <div className="py-2 px-2 space-y-0.5">
        {bottomNavItems.map((item) => {
          const Icon = item.icon;
          const isActive = location.pathname === item.path;

          return (
            <Link
              key={item.path}
              to={item.path}
              onClick={onClose}
              className={cn(
                'flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                isActive
                  ? 'bg-accent text-foreground'
                  : 'text-muted-foreground hover:text-foreground hover:bg-accent/50'
              )}
            >
              <Icon className="w-4 h-4" />
              {item.label}
            </Link>
          );
        })}
      </div>
    </aside>
  );
}

function Header() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const { connected } = useWebSocket();

  return (
    <>
      <header className="h-12 border-b border-border flex items-center justify-between px-4 sticky top-0 z-30 bg-background">
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden h-8 w-8"
            onClick={() => setMobileMenuOpen(true)}
          >
            <Menu className="w-4 h-4" />
          </Button>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className={cn(
              'w-1.5 h-1.5 rounded-full',
              connected ? 'bg-emerald-500' : 'bg-red-500'
            )} />
            <span className="hidden sm:inline">
              {connected ? 'Connected' : 'Disconnected'}
            </span>
          </div>
        </div>
      </header>

      {/* Mobile menu */}
      {mobileMenuOpen && (
        <>
          <div
            className="fixed inset-0 bg-black/50 z-40 lg:hidden"
            onClick={() => setMobileMenuOpen(false)}
          />
          <div className="fixed left-0 top-0 h-full z-50 lg:hidden">
            <Sidebar mobile onClose={() => setMobileMenuOpen(false)} />
          </div>
        </>
      )}
    </>
  );
}

export function Layout() {
  useWebSocket();

  return (
    <div className="min-h-screen bg-background flex">
      <div className="hidden lg:block">
        <Sidebar />
      </div>
      <div className="flex-1 flex flex-col min-w-0">
        <Header />
        <main className="flex-1 overflow-auto">
          <div className="mx-auto max-w-[1600px]">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

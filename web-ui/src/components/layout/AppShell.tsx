import { Outlet, useLocation } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { useHealthWebSocket } from '@/hooks/useHealthWebSocket';

/** Routes where we suppress the top Header bar (full-screen chat layout) */
const HEADERLESS_ROUTES = ['/agents/'];

function useHeaderless(): boolean {
  const { pathname } = useLocation();
  return HEADERLESS_ROUTES.some((prefix) => pathname.startsWith(prefix));
}

export function AppShell() {
  useHealthWebSocket(true);
  const headerless = useHeaderless();

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden min-w-0">
        {!headerless && <Header />}
        <main className={headerless ? 'flex-1 overflow-hidden' : 'flex-1 overflow-y-auto p-6'}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

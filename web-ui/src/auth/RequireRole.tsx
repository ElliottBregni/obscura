import { type ReactNode } from 'react';
import { useAuth } from './useAuth';
import { useSystemStore } from '@/stores/systemStore';

interface RequireRoleProps {
  role?: string;
  roles?: string[];
  children: ReactNode;
  fallback?: ReactNode;
}

export function RequireRole({ role, roles, children, fallback = null }: RequireRoleProps) {
  const { hasRole, hasAnyRole } = useAuth();
  const authEnabled = useSystemStore((s) => s.authEnabled);

  // When auth is disabled, allow all access
  if (!authEnabled) return <>{children}</>;

  if (role && !hasRole(role)) return <>{fallback}</>;
  if (roles && roles.length > 0 && !hasAnyRole(...roles)) return <>{fallback}</>;

  return <>{children}</>;
}

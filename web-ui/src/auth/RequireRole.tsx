import { type ReactNode } from 'react';
import { useAuth } from './useAuth';

interface RequireRoleProps {
  role?: string;
  roles?: string[];
  children: ReactNode;
  fallback?: ReactNode;
}

export function RequireRole({ role, roles, children, fallback = null }: RequireRoleProps) {
  const { hasRole, hasAnyRole } = useAuth();

  if (role && !hasRole(role)) return <>{fallback}</>;
  if (roles && roles.length > 0 && !hasAnyRole(...roles)) return <>{fallback}</>;

  return <>{children}</>;
}

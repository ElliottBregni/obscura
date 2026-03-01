import { useAuthStore } from '@/stores/authStore';

export function useAuth() {
  const { user, isAuthenticated, hasRole, hasAnyRole, logout, setToken, setApiKey } =
    useAuthStore();

  return {
    user,
    isAuthenticated,
    hasRole,
    hasAnyRole,
    logout,
    setToken,
    setApiKey,
    roles: user?.roles ?? [],
  };
}

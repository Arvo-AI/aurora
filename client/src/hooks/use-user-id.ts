"use client";

import { useUser } from '@/hooks/useAuthHooks';

/**
 * Hook to get user ID from Auth.js session
 * Simplified - Auth.js authentication only
 */
export function useUserId() {
  const { user, isLoaded } = useUser();
  const userId = user?.id ?? null;

  return {
    userId,
    isGuest: false, // Auth.js authentication only
    isLoading: !isLoaded,
    error: null,
    isAuthenticated: !!userId,
  };
}

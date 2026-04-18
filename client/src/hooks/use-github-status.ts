import { useState, useEffect, useCallback, useRef } from 'react';
import { GitHubIntegrationService } from '@/components/github-provider-integration';

interface GitHubStatus {
  isAuthenticated: boolean;
  isConnected: boolean;
  hasReposConnected: boolean | null;
  username?: string;
}

/**
 * Single source of truth for GitHub connection status.
 * - isAuthenticated: OAuth credentials exist
 * - isConnected: OAuth done AND at least one repo connected
 */
export function useGitHubStatus(userId: string | null) {
  const [status, setStatus] = useState<GitHubStatus>({
    isAuthenticated: false,
    isConnected: false,
    hasReposConnected: null,
  });
  const inFlightRef = useRef(false);

  const checkStatus = useCallback(async () => {
    if (!userId || inFlightRef.current) return;
    inFlightRef.current = true;

    try {
      const [credentials, repos] = await Promise.all([
        GitHubIntegrationService.checkStatus(),
        GitHubIntegrationService.fetchRepoSelections().catch(() => []),
      ]);

      const isAuthenticated = credentials.connected || false;
      if (!isAuthenticated) {
        setStatus({ isAuthenticated: false, isConnected: false, hasReposConnected: false });
        return;
      }

      const hasReposConnected = repos.length > 0;
      setStatus({
        isAuthenticated: true,
        isConnected: hasReposConnected,
        hasReposConnected,
        username: credentials.username,
      });
    } catch {
      setStatus({ isAuthenticated: false, isConnected: false, hasReposConnected: null });
    } finally {
      inFlightRef.current = false;
    }
  }, [userId]);

  useEffect(() => { checkStatus(); }, [checkStatus]);

  useEffect(() => {
    const handleProviderChange = () => { checkStatus(); };
    window.addEventListener('providerStateChanged', handleProviderChange);
    return () => { window.removeEventListener('providerStateChanged', handleProviderChange); };
  }, [checkStatus]);

  return { ...status, refresh: checkStatus };
}

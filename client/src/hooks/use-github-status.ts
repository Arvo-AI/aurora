import { useState, useEffect, useCallback } from 'react';
import { GitHubIntegrationService } from '@/components/github-provider-integration';

interface GitHubStatus {
  isAuthenticated: boolean; // OAuth done
  isConnected: boolean; // OAuth done AND repo selected
  hasRepoSelected: boolean | null; // null = unknown, true/false = known
  username?: string;
}

/**
 * Single source of truth for GitHub connection status
 * - isAuthenticated: OAuth credentials exist
 * - isConnected: OAuth done AND repository selected
 */
export function useGitHubStatus(userId: string | null) {
  const [status, setStatus] = useState<GitHubStatus>({
    isAuthenticated: false,
    isConnected: false,
    hasRepoSelected: null,
  });

  const checkStatus = useCallback(async () => {
    if (!userId) {
      setStatus({
        isAuthenticated: false,
        isConnected: false,
        hasRepoSelected: null,
      });
      return;
    }

    try {
      // Check OAuth status
      const credentials = await GitHubIntegrationService.checkStatus(userId);
      const isAuthenticated = credentials.connected || false;

      if (!isAuthenticated) {
        setStatus({
          isAuthenticated: false,
          isConnected: false,
          hasRepoSelected: false,
        });
        return;
      }

      // Check repo selection
      const repoSelection = await GitHubIntegrationService.loadRepoSelection(userId);
      const hasRepoSelected = repoSelection !== null && 
                              repoSelection.repository !== null && 
                              repoSelection.branch !== null;

      setStatus({
        isAuthenticated: true,
        isConnected: hasRepoSelected,
        hasRepoSelected,
        username: credentials.username,
      });
    } catch (error) {
      console.error('Error checking GitHub status:', error);
      setStatus({
        isAuthenticated: false,
        isConnected: false,
        hasRepoSelected: null,
      });
    }
  }, [userId]);

  // Initial check and on userId change
  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

  // Listen for provider state changes
  useEffect(() => {
    const handleProviderChange = () => {
      checkStatus();
    };

    window.addEventListener('providerStateChanged', handleProviderChange);
    return () => {
      window.removeEventListener('providerStateChanged', handleProviderChange);
    };
  }, [checkStatus]);

  return {
    ...status,
    refresh: checkStatus,
  };
}

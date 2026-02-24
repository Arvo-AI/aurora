import { useState, useEffect, useCallback } from 'react';
import { BitbucketIntegrationService } from '@/components/bitbucket-provider-integration';

interface BitbucketStatus {
  isAuthenticated: boolean;
  isConnected: boolean;
  hasWorkspaceSelected: boolean | null;
  username?: string;
  displayName?: string;
}

/**
 * Single source of truth for Bitbucket connection status
 * - isAuthenticated: credentials exist
 * - isConnected: credentials exist AND workspace+repo+branch selected
 */
export function useBitbucketStatus(userId: string | null) {
  const [status, setStatus] = useState<BitbucketStatus>({
    isAuthenticated: false,
    isConnected: false,
    hasWorkspaceSelected: null,
  });

  const checkStatus = useCallback(async () => {
    if (!userId) {
      setStatus({
        isAuthenticated: false,
        isConnected: false,
        hasWorkspaceSelected: null,
      });
      return;
    }

    try {
      const credentials = await BitbucketIntegrationService.checkStatus(userId);
      const isAuthenticated = credentials.connected || false;

      if (!isAuthenticated) {
        setStatus({
          isAuthenticated: false,
          isConnected: false,
          hasWorkspaceSelected: false,
        });
        return;
      }

      const selection = await BitbucketIntegrationService.loadWorkspaceSelection(userId);
      const hasWorkspaceSelected = selection !== null &&
        selection.workspace !== null &&
        selection.repository !== null &&
        selection.branch !== null;

      setStatus({
        isAuthenticated: true,
        isConnected: hasWorkspaceSelected,
        hasWorkspaceSelected,
        username: credentials.username,
        displayName: credentials.display_name,
      });
    } catch (error) {
      console.error('Error checking Bitbucket status:', error);
      setStatus({
        isAuthenticated: false,
        isConnected: false,
        hasWorkspaceSelected: null,
      });
    }
  }, [userId]);

  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

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

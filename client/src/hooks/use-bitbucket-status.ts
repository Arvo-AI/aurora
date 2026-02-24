import { useState, useEffect, useCallback } from 'react';
import { BitbucketIntegrationService } from '@/components/bitbucket-provider-integration';

interface BitbucketStatus {
  isAuthenticated: boolean;
  isConnected: boolean;
  hasWorkspaceSelected: boolean | null;
  username?: string;
  displayName?: string;
}

const DISCONNECTED_STATUS: BitbucketStatus = {
  isAuthenticated: false,
  isConnected: false,
  hasWorkspaceSelected: null,
};

/**
 * Single source of truth for Bitbucket connection status.
 * - isAuthenticated: credentials exist
 * - isConnected: credentials exist AND workspace+repo+branch selected
 */
export function useBitbucketStatus(userId: string | null) {
  const [status, setStatus] = useState<BitbucketStatus>(DISCONNECTED_STATUS);

  const checkStatus = useCallback(async () => {
    if (!userId) {
      setStatus(DISCONNECTED_STATUS);
      return;
    }

    try {
      const credentials = await BitbucketIntegrationService.checkStatus(userId);
      if (!credentials.connected) {
        setStatus({ ...DISCONNECTED_STATUS, hasWorkspaceSelected: false });
        return;
      }

      const selection = await BitbucketIntegrationService.loadWorkspaceSelection(userId);
      const hasWorkspaceSelected = Boolean(
        selection?.workspace && selection?.repository && selection?.branch
      );

      setStatus({
        isAuthenticated: true,
        isConnected: hasWorkspaceSelected,
        hasWorkspaceSelected,
        username: credentials.username,
        displayName: credentials.display_name,
      });
    } catch (error) {
      console.error('Error checking Bitbucket status:', error);
      setStatus(DISCONNECTED_STATUS);
    }
  }, [userId]);

  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

  useEffect(() => {
    window.addEventListener('providerStateChanged', checkStatus);
    return () => window.removeEventListener('providerStateChanged', checkStatus);
  }, [checkStatus]);

  return {
    ...status,
    refresh: checkStatus,
  };
}

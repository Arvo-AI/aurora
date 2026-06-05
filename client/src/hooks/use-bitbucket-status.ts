import { useState, useEffect, useCallback, useRef } from 'react';
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
 * - isConnected: credentials exist AND workspace + at least one repo selected
 */
export function useBitbucketStatus() {
  const [status, setStatus] = useState<BitbucketStatus>(DISCONNECTED_STATUS);
  const inFlightRef = useRef(false);
  const pendingRef = useRef(false);

  const checkStatus = useCallback(async () => {
    if (inFlightRef.current) {
      pendingRef.current = true;
      return;
    }
    inFlightRef.current = true;

    do {
      pendingRef.current = false;
      try {
        const [credentials, selection] = await Promise.all([
          BitbucketIntegrationService.checkStatus(),
          BitbucketIntegrationService.loadWorkspaceSelection().catch(() => null),
        ]);

        if (!credentials.connected) {
          setStatus({ ...DISCONNECTED_STATUS, hasWorkspaceSelected: false });
        } else {
          const hasWorkspaceSelected = Boolean(
            selection?.workspace && Array.isArray(selection?.repositories) && selection.repositories.length > 0
          );

          setStatus({
            isAuthenticated: true,
            isConnected: hasWorkspaceSelected,
            hasWorkspaceSelected,
            username: credentials.username,
            displayName: credentials.display_name,
          });
        }
      } catch (error) {
        console.error('Error checking Bitbucket status:', error);
        setStatus(DISCONNECTED_STATUS);
      }
    } while (pendingRef.current);

    inFlightRef.current = false;
  }, []);

  useEffect(() => { checkStatus(); }, [checkStatus]);

  useEffect(() => {
    const handler = () => { checkStatus(); };
    window.addEventListener('providerStateChanged', handler);
    window.addEventListener('focus', handler);
    return () => {
      window.removeEventListener('providerStateChanged', handler);
      window.removeEventListener('focus', handler);
    };
  }, [checkStatus]);

  return {
    ...status,
    refresh: checkStatus,
  };
}

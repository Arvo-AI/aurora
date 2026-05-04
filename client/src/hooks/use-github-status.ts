import { useState, useEffect, useCallback, useRef } from 'react';
import { GitHubIntegrationService } from '@/components/github-provider-integration';
import type { GitHubInstallation } from '@/lib/github-app';

interface GitHubStatus {
  isAuthenticated: boolean;
  isConnected: boolean;
  hasReposConnected: boolean | null;
  username?: string;
}

export type InstallationState = 'ok' | 'suspended' | 'pending_permissions' | 'no_repos';

type InstallationRepoLike = { installation_id?: number | null };

/**
 * Derive an installation's state from its record + caller-supplied repo list.
 * Precedence (per Task 18 spec): suspended > pending_permissions > no_repos > ok.
 * `reposLoaded` MUST be true before `no_repos` can be reported, otherwise we'd
 * surface a false-positive banner during the lazy-load window.
 */
export function computeInstallationState(
  installation: GitHubInstallation,
  repos: InstallationRepoLike[],
  options: { reposLoaded?: boolean } = {}
): InstallationState {
  const { reposLoaded = true } = options;
  if (installation.suspended_at) return 'suspended';
  if (installation.permissions_pending_update) return 'pending_permissions';
  if (
    installation.repository_selection === 'selected' &&
    reposLoaded &&
    repos.filter(r => r.installation_id === installation.installation_id).length === 0
  ) {
    return 'no_repos';
  }
  return 'ok';
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
    if (inFlightRef.current) return;
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
  }, []);

  useEffect(() => { checkStatus(); }, [checkStatus]);

  useEffect(() => {
    const handleProviderChange = () => { checkStatus(); };
    window.addEventListener('providerStateChanged', handleProviderChange);
    return () => { window.removeEventListener('providerStateChanged', handleProviderChange); };
  }, [checkStatus]);

  return { ...status, refresh: checkStatus };
}

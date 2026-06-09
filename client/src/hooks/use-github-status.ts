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
  const { reposLoaded = false } = options;
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
export function useGitHubStatus() {
  const [status, setStatus] = useState<GitHubStatus>({
    isAuthenticated: false,
    isConnected: false,
    hasReposConnected: null,
  });
  // Coalesce overlapping refresh calls without dropping them. While a
  // check is in-flight, additional refresh() calls flip pendingRef so
  // the current check re-runs once it finishes — the user's "needs a
  // page refresh" symptom was caused by silently dropping these.
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
        const [credentials, repos] = await Promise.all([
          GitHubIntegrationService.checkStatus(),
          GitHubIntegrationService.fetchRepoSelections().catch(() => []),
        ]);

        const isAuthenticated = credentials.connected || false;
        if (!isAuthenticated) {
          setStatus({ isAuthenticated: false, isConnected: false, hasReposConnected: false });
        } else {
          const hasReposConnected = repos.length > 0;
          setStatus({
            isAuthenticated: true,
            isConnected: hasReposConnected,
            hasReposConnected,
            username: credentials.username,
          });
        }
      } catch {
        setStatus({ isAuthenticated: false, isConnected: false, hasReposConnected: null });
      }
    } while (pendingRef.current);

    inFlightRef.current = false;
  }, []);

  useEffect(() => { checkStatus(); }, [checkStatus]);

  useEffect(() => {
    const allowedOrigins = new Set<string>();
    allowedOrigins.add(window.location.origin);
    const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || '';
    if (backendUrl) {
      try { allowedOrigins.add(new URL(backendUrl).origin); } catch { /* ignore */ }
    }
    const handleProviderChange = () => { checkStatus(); };
    const handleAuthMessage = (event: MessageEvent) => {
      if (!allowedOrigins.has(event.origin)) return;
      const data = event.data as { type?: string } | null;
      if (data && data.type === 'github_auth_success') {
        checkStatus();
      }
    };
    window.addEventListener('providerStateChanged', handleProviderChange);
    window.addEventListener('message', handleAuthMessage);
    return () => {
      window.removeEventListener('providerStateChanged', handleProviderChange);
      window.removeEventListener('message', handleAuthMessage);
    };
  }, [checkStatus]);

  return { ...status, refresh: checkStatus };
}

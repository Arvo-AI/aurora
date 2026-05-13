import { useState, useEffect, useCallback, useRef } from 'react';

interface GitLabStatus {
  isConnected: boolean;
  username?: string;
  baseUrl?: string;
  hasProjectsConnected: boolean | null;
}

export function useGitLabStatus(userId: string | null) {
  const [status, setStatus] = useState<GitLabStatus>({
    isConnected: false,
    hasProjectsConnected: null,
  });
  const inFlightRef = useRef(false);

  const checkStatus = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;

    try {
      const [credRes, repoRes] = await Promise.all([
        fetch('/api/proxy/gitlab/status'),
        fetch('/api/proxy/gitlab/repo-selections').catch(() => null),
      ]);

      const credentials = credRes.ok ? await credRes.json() : { connected: false };
      const repoData = repoRes && repoRes.ok ? await repoRes.json() : { repositories: [] };
      const repos = repoData.repositories || [];

      const isAuthenticated = credentials.connected || false;
      if (!isAuthenticated) {
        setStatus({ isConnected: false, hasProjectsConnected: false });
        return;
      }

      const hasProjectsConnected = repos.length > 0;
      setStatus({
        isConnected: hasProjectsConnected,
        hasProjectsConnected,
        username: credentials.username,
        baseUrl: credentials.base_url,
      });
    } catch {
      setStatus({ isConnected: false, hasProjectsConnected: null });
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

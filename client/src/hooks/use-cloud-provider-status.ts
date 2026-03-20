import { useState, useEffect, useCallback } from 'react';

/**
 * Hook to check if user is logged into any cloud provider.
 * Uses /api/connected-accounts as the single source of truth (database-backed,
 * org-aware) instead of localStorage, so org-shared connections are visible
 * to all members.
 */
export function useCloudProviderStatus() {
  const [connectedProviders, setConnectedProviders] = useState<Record<string, boolean>>({});
  const [isLoading, setIsLoading] = useState(true);

  const fetchStatus = useCallback(async () => {
    try {
      const response = await fetch('/api/connected-accounts', {
        credentials: 'include',
      });
      if (!response.ok) {
        console.error('Failed to fetch connected accounts:', response.status);
        return;
      }
      const data = await response.json();
      const accounts = data.accounts || {};

      const status: Record<string, boolean> = {};
      for (const key of Object.keys(accounts)) {
        status[key.toLowerCase()] = true;
      }
      setConnectedProviders(status);
    } catch (error) {
      console.error('Error fetching cloud provider status:', error);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStatus();

    const handleProviderStateChange = () => {
      fetchStatus();
    };

    window.addEventListener('providerStateChanged', handleProviderStateChange);
    window.addEventListener('providerConnectionAction', handleProviderStateChange);

    return () => {
      window.removeEventListener('providerStateChanged', handleProviderStateChange);
      window.removeEventListener('providerConnectionAction', handleProviderStateChange);
    };
  }, [fetchStatus]);

  const isGCPConnected = connectedProviders['gcp'] ?? false;
  const isAWSConnected = connectedProviders['aws'] ?? false;
  const isAzureConnected = connectedProviders['azure'] ?? false;
  const isOVHConnected = connectedProviders['ovh'] ?? false;
  const isScalewayConnected = connectedProviders['scaleway'] ?? false;
  const isTailscaleConnected = connectedProviders['tailscale'] ?? false;
  const isGrafanaConnected = connectedProviders['grafana'] ?? false;
  const isDatadogConnected = connectedProviders['datadog'] ?? false;
  const isNetdataConnected = connectedProviders['netdata'] ?? false;
  const isSplunkConnected = connectedProviders['splunk'] ?? false;
  const isDynatraceConnected = connectedProviders['dynatrace'] ?? false;
  const isPagerDutyConnected = connectedProviders['pagerduty'] ?? false;
  const isKubectlConnected = connectedProviders['kubectl'] ?? false;
  const isGitHubConnected = connectedProviders['github'] ?? false;

  const anyProviderConnected = Object.values(connectedProviders).some(Boolean);

  return {
    isGCPConnected,
    isAWSConnected,
    isAzureConnected,
    isOVHConnected,
    isScalewayConnected,
    isTailscaleConnected,
    isGrafanaConnected,
    isDatadogConnected,
    isNetdataConnected,
    isSplunkConnected,
    isDynatraceConnected,
    isPagerDutyConnected,
    isKubectlConnected,
    isGitHubConnected,
    anyProviderConnected,
    isLoading,
  };
}

import { useState, useEffect } from 'react';
import {
  fetchConnectedAccounts,
  getConnectedAccounts,
  subscribe,
} from '@/lib/connected-accounts-cache';

/**
 * Hook to check if user is logged into any cloud provider.
 * Reads from a shared in-memory cache (single fetch for all consumers).
 */
export function useCloudProviderStatus() {
  const [connectedProviders, setConnectedProviders] = useState<Record<string, boolean>>({});
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const sync = () => {
      const { providerIds } = getConnectedAccounts();
      const status: Record<string, boolean> = {};
      for (const id of providerIds) status[id] = true;
      setConnectedProviders(status);
      setIsLoading(false);
    };

    fetchConnectedAccounts().then(sync).catch(() => setIsLoading(false));

    const unsub = subscribe(sync);
    return unsub;
  }, []);

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

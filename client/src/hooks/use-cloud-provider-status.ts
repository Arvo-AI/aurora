import { useState, useEffect } from 'react';

/**
 * Hook to check if user is logged into any cloud provider
 * @returns Object containing login status for each provider and if any provider is connected
 */
export function useCloudProviderStatus() {
  const [isGCPConnected, setIsGCPConnected] = useState(false);
  const [isAWSConnected, setIsAWSConnected] = useState(false);
  const [isAzureConnected, setIsAzureConnected] = useState(false);
  const [isOVHConnected, setIsOVHConnected] = useState(false);
  const [isScalewayConnected, setIsScalewayConnected] = useState(false);
  const [isTailscaleConnected, setIsTailscaleConnected] = useState(false);
  const [isGrafanaConnected, setIsGrafanaConnected] = useState(false);
  const [isDatadogConnected, setIsDatadogConnected] = useState(false);
  const [isGitHubConnected, setIsGitHubConnected] = useState(false);
  const [isNetdataConnected, setIsNetdataConnected] = useState(false);
  const [isSplunkConnected, setIsSplunkConnected] = useState(false);
  const [isPagerDutyConnected, setIsPagerDutyConnected] = useState(false);
  const [isKubectlConnected, setIsKubectlConnected] = useState(false);
  const [anyProviderConnected, setAnyProviderConnected] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const updateProviderStatus = () => {
      const getGitHubConnected = () => {
        const cachedData = localStorage.getItem('github_cached_data');
        if (!cachedData) return false;
        try {
          const parsed = JSON.parse(cachedData);
          return Boolean(parsed?.connected);
        } catch (error) {
          console.warn('Failed to parse github_cached_data:', error);
          return false;
        }
      };

      const gcpConnected = localStorage.getItem('isGCPConnected') === 'true';
      const awsConnected = localStorage.getItem('isAWSConnected') === 'true';
      const azureConnected = localStorage.getItem('isAzureConnected') === 'true';
      const ovhConnected = localStorage.getItem('isOVHConnected') === 'true';
      const scalewayConnected = localStorage.getItem('isScalewayConnected') === 'true';
      const tailscaleConnected = localStorage.getItem('isTailscaleConnected') === 'true';
      const grafanaConnected = localStorage.getItem('isGrafanaConnected') === 'true';
      const datadogConnected = localStorage.getItem('isDatadogConnected') === 'true';
      const netdataConnected = localStorage.getItem('isNetdataConnected') === 'true';
      const splunkConnected = localStorage.getItem('isSplunkConnected') === 'true';
      const pagerdutyConnected = localStorage.getItem('isPagerDutyConnected') === 'true';
      const kubectlConnected = localStorage.getItem('isKubectlConnected') === 'true';
      const githubConnected = getGitHubConnected();
      
      setIsGCPConnected(gcpConnected);
      setIsAWSConnected(awsConnected);
      setIsAzureConnected(azureConnected);
      setIsOVHConnected(ovhConnected);
      setIsScalewayConnected(scalewayConnected);
      setIsTailscaleConnected(tailscaleConnected);
      setIsGrafanaConnected(grafanaConnected);
      setIsDatadogConnected(datadogConnected);
      setIsNetdataConnected(netdataConnected);
      setIsSplunkConnected(splunkConnected);
      setIsPagerDutyConnected(pagerdutyConnected);
      setIsKubectlConnected(kubectlConnected);
      setIsGitHubConnected(githubConnected);
      setAnyProviderConnected(
        gcpConnected ||
        awsConnected ||
        azureConnected ||
        ovhConnected ||
        scalewayConnected ||
        tailscaleConnected ||
        grafanaConnected ||
        datadogConnected ||
        netdataConnected ||
        splunkConnected ||
        pagerdutyConnected ||
        kubectlConnected ||
        githubConnected
      );
    };

    // Initial check on mount
    updateProviderStatus();

    // Event listeners for changes (no polling)
    const handleStorageChange = (e: StorageEvent) => {
      if (
        e.key?.includes('Connected') ||
        e.key === 'isGrafanaConnected' ||
        e.key === 'github_cached_data' ||
        e.key === 'github_last_checked'
      ) {
        updateProviderStatus();
      }
    };

    const handleProviderStateChange = () => {
      updateProviderStatus();
    };

    const handleProviderConnectionAction = () => {
      updateProviderStatus();
    };

    // Listen for localStorage changes from other tabs
    window.addEventListener('storage', handleStorageChange);
    
    // Listen for provider state changes from same tab
    window.addEventListener('providerStateChanged', handleProviderStateChange);
    
    // Listen for provider connection actions
    window.addEventListener('providerConnectionAction', handleProviderConnectionAction);

    return () => {
      window.removeEventListener('storage', handleStorageChange);
      window.removeEventListener('providerStateChanged', handleProviderStateChange);
      window.removeEventListener('providerConnectionAction', handleProviderConnectionAction);
    };
  }, []); // Empty dependency array - only run once on mount

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
    isPagerDutyConnected,
    isKubectlConnected,
    isGitHubConnected,
    anyProviderConnected
  };
} 

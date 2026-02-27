import { useState, useEffect } from "react";
import { GitHubIntegrationService } from "@/components/github-provider-integration";
import { BitbucketIntegrationService } from "@/components/bitbucket-provider-integration";
import { isSlackEnabled, isOvhEnabled, isScalewayEnabled } from "@/lib/feature-flags";
import { getEnv } from '@/lib/env';
import type { ConnectorConfig } from "@/components/connectors/types";

const slackService = isSlackEnabled() ? require("@/lib/services/slack").slackService : null;
const pagerdutyService = require("@/lib/services/pagerduty").pagerdutyService;

export function useConnectorStatus(connector: ConnectorConfig, userId: string | null) {
  const [isConnected, setIsConnected] = useState(false);
  const [isCheckingConnection, setIsCheckingConnection] = useState(true);
  const [isLoadingDetails, setIsLoadingDetails] = useState(false);
  const [slackStatus, setSlackStatus] = useState<any>(null);

  useEffect(() => {
    checkConnectionStatus();
    
    const handleProviderChange = () => {
      if (connector.id === "slack" && typeof window !== "undefined") {
        const isSlackConnected = localStorage.getItem('isSlackConnected') === 'true';
        if (isSlackConnected) {
          setIsConnected(true);
          setIsLoadingDetails(true);
          if (userId) checkSlackStatus();
          return;
        }
      }
      if (connector.id === "pagerduty" && typeof window !== "undefined") {
        const isPagerDutyConnected = localStorage.getItem('isPagerDutyConnected') === 'true';
        if (isPagerDutyConnected) {
          setIsConnected(true);
          setIsLoadingDetails(true);
          if (userId) checkPagerDutyStatus();
          return;
        }
      }
      if (connector.id === "onprem" && typeof window !== "undefined") {
        checkVmConfigStatus();
        return;
      }
      if (connector.useCustomConnection && (connector.id === "gcp" || connector.id === "slack")) {
        checkApiConnectionStatus();
        return;
      }
      checkConnectionStatus();
    };

    window.addEventListener("providerStateChanged", handleProviderChange);
    return () => window.removeEventListener("providerStateChanged", handleProviderChange);
  }, [connector.id, userId]);

  useEffect(() => {
    if (connector.id === "github" && userId) {
      checkGitHubStatus();
    }
    if (connector.id === "slack" && userId) {
      const isSlackConnected = typeof window !== "undefined" && localStorage.getItem('isSlackConnected') === 'true';
      if (isSlackConnected) {
        setIsConnected(true);
        setIsLoadingDetails(true);
      }
      checkSlackStatus();
    }
    if (connector.id === "pagerduty" && userId) {
      const isPagerDutyConnected = typeof window !== "undefined" && localStorage.getItem('isPagerDutyConnected') === 'true';
      if (isPagerDutyConnected) {
        setIsConnected(true);
        setIsLoadingDetails(true);
      }
      checkPagerDutyStatus();
    }
    if (connector.id === "bitbucket" && userId) {
      checkBitbucketStatus();
    }
    if (connector.id === "onprem" && userId) {
      // Don't trust localStorage - always verify with API
      checkVmConfigStatus();
    }
    if (connector.useCustomConnection && connector.id === "gcp") {
      checkApiConnectionStatus();
    }
  }, [userId, connector.id]);

  const checkGitHubStatus = async () => {
    if (!userId) return;
    
    try {
      const data = await GitHubIntegrationService.checkStatus(userId);
      setIsConnected(data.connected || false);
      localStorage.setItem('github_cached_data', JSON.stringify(data));
      localStorage.setItem('github_last_checked', Date.now().toString());
      // Don't dispatch providerStateChanged here - it causes infinite loop on connectors page
      // Only dispatch when explicitly disconnecting/connecting (handled in github-auth.tsx and github-settings.tsx)
    } catch (error) {
      console.error("Error checking GitHub status:", error);
      setIsConnected(false);
    }
  };

  const checkBitbucketStatus = async () => {
    if (!userId) return;

    try {
      const data = await BitbucketIntegrationService.checkStatus(userId);
      setIsConnected(data.connected || false);
    } catch (error) {
      console.error("Error checking Bitbucket status:", error);
      setIsConnected(false);
    }
  };

  const checkSlackStatus = async () => {
    setIsLoadingDetails(true);
    try {
      const data = await slackService.getStatus();
      const connected = data?.connected || false;
      setIsConnected(connected);
      setSlackStatus(data);
      if (typeof window !== "undefined") {
        if (connected) {
          localStorage.setItem('isSlackConnected', 'true');
        } else {
          localStorage.removeItem('isSlackConnected');
        }
      }
    } catch (error) {
      console.error("Error checking Slack status:", error);
      setIsConnected(false);
      setSlackStatus(null);
      if (typeof window !== "undefined") {
        localStorage.removeItem('isSlackConnected');
      }
    } finally {
      setIsLoadingDetails(false);
    }
  };

  const checkPagerDutyStatus = async () => {
    setIsLoadingDetails(true);
    try {
      const data = await pagerdutyService.getStatus();
      const connected = data?.connected || false;
      setIsConnected(connected);
      if (typeof window !== "undefined") {
        if (connected) {
          localStorage.setItem('isPagerDutyConnected', 'true');
        } else {
          localStorage.removeItem('isPagerDutyConnected');
        }
      }
    } catch (error) {
      console.error("Error checking PagerDuty status:", error);
      setIsConnected(false);
      if (typeof window !== "undefined") {
        localStorage.removeItem('isPagerDutyConnected');
      }
    } finally {
      setIsLoadingDetails(false);
    }
  };

  const checkVmConfigStatus = async () => {
    setIsCheckingConnection(true);
    try {
      const manualResponse = await fetch('/api/vms/manual', {
        credentials: 'include',
      });
      
      if (manualResponse.ok) {
        const manualData = await manualResponse.json();
        const hasVerifiedManualVm = (manualData.vms || []).some((vm: any) => vm.connectionVerified);
        if (hasVerifiedManualVm) {
          setIsConnected(true);
          if (typeof window !== "undefined") {
            localStorage.setItem('isOnPremConnected', 'true');
          }
          return;
        }
      }
      
      const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL');
      if (!backendUrl || !userId) {
        setIsConnected(false);
        if (typeof window !== "undefined") {
          localStorage.removeItem('isOnPremConnected');
        }
        return;
      }
      
      if (isOvhEnabled()) {
        try {
          const ovhResponse = await fetch(`${backendUrl}/ovh_api/ovh/instances`, {
            headers: { "X-User-ID": userId },
            credentials: "include",
          });
          if (ovhResponse.ok) {
            const ovhData = await ovhResponse.json();
            const hasConfiguredOvhVm = (ovhData.instances || []).some((instance: any) => instance.sshConfig);
            if (hasConfiguredOvhVm) {
              setIsConnected(true);
              if (typeof window !== "undefined") {
                localStorage.setItem('isOnPremConnected', 'true');
              }
              return;
            }
          }
        } catch {
          /* OVH not configured - continue to next provider */
        }
      }
      
      if (isScalewayEnabled()) {
        try {
          const scwResponse = await fetch(`${backendUrl}/scaleway_api/scaleway/instances`, {
            headers: { "X-User-ID": userId },
            credentials: "include",
          });
          if (scwResponse.ok) {
            const scwData = await scwResponse.json();
            const hasConfiguredScwVm = (scwData.servers || []).some((server: any) => server.sshConfig);
            if (hasConfiguredScwVm) {
              setIsConnected(true);
              if (typeof window !== "undefined") {
                localStorage.setItem('isOnPremConnected', 'true');
              }
              return;
            }
          }
        } catch {
          /* Scaleway not configured - continue */
        }
      }
      
      setIsConnected(false);
      if (typeof window !== "undefined") {
        localStorage.removeItem('isOnPremConnected');
      }
    } catch (error) {
      console.error("Error checking VM config status:", error);
      setIsConnected(false);
      if (typeof window !== "undefined") {
        localStorage.removeItem('isOnPremConnected');
      }
    } finally {
      setIsCheckingConnection(false);
    }
  };

  const checkApiConnectionStatus = async () => {
    if (connector.useCustomConnection && (connector.id === "gcp" || connector.id === "slack")) {
      try {
        const response = await fetch('/api/connected-accounts', {
          credentials: 'include',
        });
        if (!response.ok) {
          console.error('Failed to fetch connected accounts:', response.status);
          const storageKey = connector.storageKey || `is${connector.name}Connected`;
          const connected = typeof window !== "undefined" ? localStorage.getItem(storageKey) === "true" : false;
          setIsConnected(connected);
          return;
        }
        const data = await response.json();
        const accounts = data.accounts || {};
        const isConnectedInDb = Object.keys(accounts).some(
          key => key.toLowerCase() === connector.id.toLowerCase()
        );
        setIsConnected(isConnectedInDb);
        if (typeof window !== "undefined") {
          const storageKey = connector.storageKey || `is${connector.name}Connected`;
          if (isConnectedInDb) {
            localStorage.setItem(storageKey, "true");
          } else {
            localStorage.removeItem(storageKey);
          }
        }
      } catch (error) {
        console.error('Error checking API connection status:', error);
        const storageKey = connector.storageKey || `is${connector.name}Connected`;
        const connected = typeof window !== "undefined" ? localStorage.getItem(storageKey) === "true" : false;
        setIsConnected(connected);
      }
    }
  };

  const checkConnectionStatus = () => {
    if (typeof window === "undefined") return;
    
    if (connector.useCustomConnection && connector.id === "gcp") {
      checkApiConnectionStatus();
      return;
    }
    
    if (connector.id === "github") {
      const cachedData = localStorage.getItem('github_cached_data');
      if (cachedData) {
        try {
          const data = JSON.parse(cachedData);
          setIsConnected(data.connected || false);
          return;
        } catch (error) {
          console.error('Error parsing GitHub cached data:', error);
        }
      }
    }
    
    if (connector.id === "slack") {
      const isSlackConnected = localStorage.getItem('isSlackConnected') === 'true';
      if (isSlackConnected) {
        setIsConnected(true);
        setIsLoadingDetails(true);
        return;
      }
    }

    if (connector.id === "pagerduty") {
      const isPagerDutyConnected = localStorage.getItem('isPagerDutyConnected') === 'true';
      if (isPagerDutyConnected) {
        setIsConnected(true);
        setIsLoadingDetails(true);
        return;
      }
    }
    
    const storageKey = connector.storageKey || `is${connector.name}Connected`;
    const connected = localStorage.getItem(storageKey) === "true";
    setIsConnected(connected);
  };

  return {
    isConnected,
    setIsConnected,
    isCheckingConnection,
    isLoadingDetails,
    slackStatus,
    checkGitHubStatus,
    checkBitbucketStatus,
    checkSlackStatus,
    checkPagerDutyStatus,
    checkVmConfigStatus,
    checkApiConnectionStatus,
    checkConnectionStatus,
  };
}

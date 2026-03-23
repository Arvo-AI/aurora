import { useState, useEffect } from "react";
import { GitHubIntegrationService } from "@/components/github-provider-integration";
import { BitbucketIntegrationService } from "@/components/bitbucket-provider-integration";
import { isOvhEnabled, isScalewayEnabled } from "@/lib/feature-flags";
import { getEnv } from '@/lib/env';
import type { ConnectorConfig } from "@/components/connectors/types";
import { slackService } from "@/lib/services/slack";

const pagerdutyService = require("@/lib/services/pagerduty").pagerdutyService;

export function useConnectorStatus(connector: ConnectorConfig, userId: string | null, connectedOverride?: boolean) {
  const [isConnected, setIsConnected] = useState(false);
  const [isCheckingConnection, setIsCheckingConnection] = useState(true);
  const [isLoadingDetails, setIsLoadingDetails] = useState(false);
  const [slackStatus, setSlackStatus] = useState<any>(null);

  const hasOverride = connectedOverride !== undefined;

  useEffect(() => {
    if (hasOverride) {
      setIsConnected(connectedOverride);
      setIsCheckingConnection(false);
      return;
    }

    checkConnectionStatus();
    
    const handleProviderChange = () => {
      if (connector.id === "onprem" && typeof window !== "undefined") {
        checkVmConfigStatus();
        return;
      }
      checkConnectionStatus();
    };

    window.addEventListener("providerStateChanged", handleProviderChange);
    return () => window.removeEventListener("providerStateChanged", handleProviderChange);
  }, [connector.id, userId, hasOverride, connectedOverride]);

  useEffect(() => {
    if (hasOverride) return;

    if (connector.id === "github" && userId) {
      checkGitHubStatus();
    }
    if (connector.id === "slack" && userId) {
      checkSlackStatus();
    }
    if (connector.id === "pagerduty" && userId) {
      checkPagerDutyStatus();
    }
    if (connector.id === "bitbucket" && userId) {
      checkBitbucketStatus();
    }
    if (connector.id === "onprem" && userId) {
      checkVmConfigStatus();
    }
  }, [userId, connector.id, hasOverride]);

  const checkGitHubStatus = async () => {
    if (!userId) return;
    
    try {
      const data = await GitHubIntegrationService.checkStatus(userId);
      setIsConnected(data.connected || false);
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
    } catch (error) {
      console.error("Error checking Slack status:", error);
      setIsConnected(false);
      setSlackStatus(null);
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
    } catch (error) {
      console.error("Error checking PagerDuty status:", error);
      setIsConnected(false);
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
          return;
        }
      }
      
      const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL');
      if (!backendUrl || !userId) {
        setIsConnected(false);
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
              return;
            }
          }
        } catch {
          /* Scaleway not configured - continue */
        }
      }
      
      setIsConnected(false);
    } catch (error) {
      console.error("Error checking VM config status:", error);
      setIsConnected(false);
    } finally {
      setIsCheckingConnection(false);
    }
  };

  /**
   * Unified API-based connection check. Fetches /api/connected-accounts
   * (the single source of truth backed by the database) and checks if this
   * connector's provider appears in the response. Works for all providers
   * regardless of connection method (OAuth, STS, API key, etc.).
   */
  const checkApiConnectionStatus = async () => {
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
      const isConnectedInDb = Object.keys(accounts).some(
        key => key.toLowerCase() === connector.id.toLowerCase()
      );
      setIsConnected(isConnectedInDb);
    } catch (error) {
      console.error('Error checking API connection status:', error);
    }
  };

  /**
   * Main entry point for checking connection status.
   * Uses /api/connected-accounts as the single source of truth for all
   * providers. Special-case connectors (github, bitbucket, onprem) that
   * have their own dedicated status APIs fall through to those.
   */
  const checkConnectionStatus = () => {
    if (typeof window === "undefined") return;
    
    // Providers with dedicated status endpoints
    if (connector.id === "github") {
      checkGitHubStatus();
      return;
    }
    if (connector.id === "bitbucket") {
      checkBitbucketStatus();
      return;
    }
    if (connector.id === "onprem") {
      checkVmConfigStatus();
      return;
    }
    if (connector.id === "slack") {
      checkSlackStatus();
      return;
    }
    if (connector.id === "pagerduty") {
      checkPagerDutyStatus();
      return;
    }

    // All other providers (including CI connectors): use the unified API
    checkApiConnectionStatus();
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

import { useState, useEffect } from "react";
import { GitHubIntegrationService } from "@/components/github-provider-integration";
import { BitbucketIntegrationService } from "@/components/bitbucket-provider-integration";
import { isOvhEnabled, isScalewayEnabled } from "@/lib/feature-flags";
import type { ConnectorConfig } from "@/components/connectors/types";
import { slackService } from "@/lib/services/slack";
import { googleChatService } from "@/lib/services/google-chat";
import {
  getConnectedAccounts,
  subscribe,
} from '@/lib/connected-accounts-cache';
import { fetchR } from '@/lib/query';

const pagerdutyService = require("@/lib/services/pagerduty").pagerdutyService;

const SPECIAL_CONNECTORS = new Set(["github", "bitbucket", "onprem", "slack", "google_chat", "pagerduty"]);

/**
 * Connection status for a single connector card.
 *
 * - connectedOverride present  → parent batch-fetched, skip all fetching
 * - standard connector          → reads from shared connected-accounts cache
 * - special connector            → dedicated status API (only on manage pages)
 */
export function useConnectorStatus(
  connector: ConnectorConfig,
  userId: string | null,
  connectedOverride?: boolean,
) {
  const [isConnected, setIsConnected] = useState(false);
  const [isCheckingConnection, setIsCheckingConnection] = useState(true);
  const [isLoadingDetails, setIsLoadingDetails] = useState(false);
  const [slackStatus, setSlackStatus] = useState<any>(null);
  const [googleChatStatus, setGoogleChatStatus] = useState<any>(null);

  const hasOverride = connectedOverride !== undefined;
  const isSpecial = SPECIAL_CONNECTORS.has(connector.id);

  useEffect(() => {
    if (hasOverride || isSpecial) return;
    const sync = () => {
      const { providerIds } = getConnectedAccounts();
      setIsConnected(providerIds.includes(connector.id.toLowerCase()));
      setIsCheckingConnection(false);
    };
    sync();
    return subscribe(sync);
  }, [connector.id, hasOverride, isSpecial]);

  useEffect(() => {
    if (!hasOverride) return;
    setIsConnected(connectedOverride);
    setIsCheckingConnection(false);
  }, [hasOverride, connectedOverride]);

  useEffect(() => {
    if (hasOverride || !isSpecial) return;
    const check = () => {
      if (connector.id === "github") checkGitHubStatus();
      else if (connector.id === "bitbucket") checkBitbucketStatus();
      else if (connector.id === "slack") checkSlackStatus();
      else if (connector.id === "google_chat") checkGoogleChatStatus();
      else if (connector.id === "pagerduty") checkPagerDutyStatus();
      else if (connector.id === "onprem") checkVmConfigStatus();
    };
    check();
    window.addEventListener("providerStateChanged", check);
    return () => window.removeEventListener("providerStateChanged", check);
  }, [connector.id, userId, hasOverride, isSpecial]);

  // Fetch display details for messaging connectors even when batch-overridden
  useEffect(() => {
    if (!connectedOverride) return;
    if (connector.id === "slack") checkSlackStatus();
    else if (connector.id === "google_chat") checkGoogleChatStatus();
  }, [connector.id, connectedOverride]);


  const checkGitHubStatus = async () => {
    if (!userId) return;
    try {
      const data = await GitHubIntegrationService.checkStatus();
      setIsConnected(data.connected || false);
    } catch {
      setIsConnected(false);
    } finally {
      setIsCheckingConnection(false);
    }
  };

  const checkBitbucketStatus = async () => {
    if (!userId) return;
    try {
      const data = await BitbucketIntegrationService.checkStatus();
      setIsConnected(data.connected || false);
    } catch {
      setIsConnected(false);
    } finally {
      setIsCheckingConnection(false);
    }
  };

  const checkSlackStatus = async () => {
    setIsLoadingDetails(true);
    try {
      const data = await slackService.getStatus();
      setIsConnected(data?.connected || false);
      setSlackStatus(data);
    } catch {
      setIsConnected(false);
      setSlackStatus(null);
    } finally {
      setIsLoadingDetails(false);
      setIsCheckingConnection(false);
    }
  };

  const checkGoogleChatStatus = async () => {
    setIsLoadingDetails(true);
    try {
      const data = await googleChatService.getStatus();
      setIsConnected(data?.connected || false);
      setGoogleChatStatus(data);
    } catch {
      setIsConnected(false);
      setGoogleChatStatus(null);
    } finally {
      setIsLoadingDetails(false);
      setIsCheckingConnection(false);
    }
  };

  const checkPagerDutyStatus = async () => {
    setIsLoadingDetails(true);
    try {
      const data = await pagerdutyService.getStatus();
      setIsConnected(data?.connected || false);
    } catch {
      setIsConnected(false);
    } finally {
      setIsLoadingDetails(false);
      setIsCheckingConnection(false);
    }
  };

  const checkVmConfigStatus = async () => {
    setIsCheckingConnection(true);
    try {
      const manualRes = await fetchR('/api/vms/manual', { credentials: 'include' });
      if (manualRes.ok) {
        const manualData = await manualRes.json();
        if ((manualData.vms || []).some((vm: any) => vm.connectionVerified)) {
          setIsConnected(true);
          return;
        }
      }

      if (!userId) { setIsConnected(false); return; }

      if (isOvhEnabled()) {
        try {
          const r = await fetchR(`/api/proxy/ovh/instances`, {
            credentials: "include",
          });
          if (r.ok) {
            const d = await r.json();
            if ((d.instances || []).some((i: any) => i.sshConfig)) { setIsConnected(true); return; }
          }
        } catch { /* not configured */ }
      }

      if (isScalewayEnabled()) {
        try {
          const r = await fetchR(`/api/proxy/scaleway/instances`, {
            credentials: "include",
          });
          if (r.ok) {
            const d = await r.json();
            if ((d.servers || []).some((s: any) => s.sshConfig)) { setIsConnected(true); return; }
          }
        } catch { /* not configured */ }
      }

      setIsConnected(false);
    } catch {
      setIsConnected(false);
    } finally {
      setIsCheckingConnection(false);
    }
  };

  const checkConnectionStatus = () => {
    if (typeof window === "undefined") return;
    if (connector.id === "github") checkGitHubStatus();
    else if (connector.id === "bitbucket") checkBitbucketStatus();
    else if (connector.id === "onprem") checkVmConfigStatus();
    else if (connector.id === "slack") checkSlackStatus();
    else if (connector.id === "google_chat") checkGoogleChatStatus();
    else if (connector.id === "pagerduty") checkPagerDutyStatus();
    else {
      const { providerIds } = getConnectedAccounts();
      setIsConnected(providerIds.includes(connector.id.toLowerCase()));
    }
  };

  return {
    isConnected,
    setIsConnected,
    isCheckingConnection,
    isLoadingDetails,
    slackStatus,
    googleChatStatus,
    checkGitHubStatus,
    checkBitbucketStatus,
    checkSlackStatus,
    checkGoogleChatStatus,
    checkPagerDutyStatus,
    checkVmConfigStatus,
    checkConnectionStatus,
  };
}

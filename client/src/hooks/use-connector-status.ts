import { useState, useEffect } from "react";
import { GitHubIntegrationService } from "@/components/github-provider-integration";
import { BitbucketIntegrationService } from "@/components/bitbucket-provider-integration";
import { isOvhEnabled, isScalewayEnabled } from "@/lib/feature-flags";
import { getEnv } from '@/lib/env';
import type { ConnectorConfig } from "@/components/connectors/types";
import { slackService } from "@/lib/services/slack";
import {
  fetchConnectedAccounts,
  getConnectedAccounts,
  subscribe,
} from '@/lib/connected-accounts-cache';

const pagerdutyService = require("@/lib/services/pagerduty").pagerdutyService;

const SPECIAL_CONNECTORS = new Set(["github", "bitbucket", "onprem", "slack", "pagerduty"]);

/**
 * Connection status for a single connector card.
 *
 * - When `connectedOverride` is provided (connectors grid page), skips
 *   all fetching — the parent already batch-fetched via /api/connectors/status.
 * - For standard connectors, reads from the shared connected-accounts cache
 *   (zero extra network requests).
 * - For special connectors (github, bitbucket, slack, pagerduty, onprem),
 *   falls through to their dedicated status APIs.
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

  const hasOverride = connectedOverride !== undefined;
  const isSpecial = SPECIAL_CONNECTORS.has(connector.id);

  // ── Standard connectors: read from shared cache ──────────────────
  useEffect(() => {
    if (hasOverride || isSpecial) return;

    const sync = () => {
      const { providerIds } = getConnectedAccounts();
      setIsConnected(providerIds.includes(connector.id.toLowerCase()));
      setIsCheckingConnection(false);
    };

    fetchConnectedAccounts().then(sync);
    return subscribe(sync);
  }, [connector.id, hasOverride, isSpecial]);

  // ── Override from parent ─────────────────────────────────────────
  useEffect(() => {
    if (!hasOverride) return;
    setIsConnected(connectedOverride);
    setIsCheckingConnection(false);
  }, [hasOverride, connectedOverride]);

  // ── Special connectors: dedicated status APIs ────────────────────
  useEffect(() => {
    if (hasOverride || !isSpecial) return;

    const check = () => {
      if (connector.id === "github") checkGitHubStatus();
      else if (connector.id === "bitbucket") checkBitbucketStatus();
      else if (connector.id === "slack") checkSlackStatus();
      else if (connector.id === "pagerduty") checkPagerDutyStatus();
      else if (connector.id === "onprem") checkVmConfigStatus();
    };

    check();

    window.addEventListener("providerStateChanged", check);
    return () => window.removeEventListener("providerStateChanged", check);
  }, [connector.id, userId, hasOverride, isSpecial]);

  // ── Status check implementations ────────────────────────────────

  const checkGitHubStatus = async () => {
    if (!userId) return;
    try {
      const data = await GitHubIntegrationService.checkStatus(userId);
      setIsConnected(data.connected || false);
    } catch {
      setIsConnected(false);
    }
  };

  const checkBitbucketStatus = async () => {
    if (!userId) return;
    try {
      const data = await BitbucketIntegrationService.checkStatus(userId);
      setIsConnected(data.connected || false);
    } catch {
      setIsConnected(false);
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
    }
  };

  const checkVmConfigStatus = async () => {
    setIsCheckingConnection(true);
    try {
      const manualResponse = await fetch('/api/vms/manual', { credentials: 'include' });
      if (manualResponse.ok) {
        const manualData = await manualResponse.json();
        if ((manualData.vms || []).some((vm: any) => vm.connectionVerified)) {
          setIsConnected(true);
          return;
        }
      }

      const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL');
      if (!backendUrl || !userId) { setIsConnected(false); return; }

      if (isOvhEnabled()) {
        try {
          const r = await fetch(`${backendUrl}/ovh_api/ovh/instances`, {
            headers: { "X-User-ID": userId }, credentials: "include",
          });
          if (r.ok) {
            const d = await r.json();
            if ((d.instances || []).some((i: any) => i.sshConfig)) { setIsConnected(true); return; }
          }
        } catch { /* not configured */ }
      }

      if (isScalewayEnabled()) {
        try {
          const r = await fetch(`${backendUrl}/scaleway_api/scaleway/instances`, {
            headers: { "X-User-ID": userId }, credentials: "include",
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

  const checkApiConnectionStatus = async () => {
    const { providerIds } = await fetchConnectedAccounts();
    setIsConnected(providerIds.includes(connector.id.toLowerCase()));
  };

  const checkConnectionStatus = () => {
    if (typeof window === "undefined") return;
    if (isSpecial) {
      if (connector.id === "github") checkGitHubStatus();
      else if (connector.id === "bitbucket") checkBitbucketStatus();
      else if (connector.id === "onprem") checkVmConfigStatus();
      else if (connector.id === "slack") checkSlackStatus();
      else if (connector.id === "pagerduty") checkPagerDutyStatus();
    } else {
      checkApiConnectionStatus();
    }
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

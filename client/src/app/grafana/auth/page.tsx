"use client";

import { useCallback, useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { grafanaService, GrafanaStatus } from "@/lib/services/grafana";
import { GrafanaConnectionStep } from "@/components/grafana/GrafanaConnectionStep";
import { GrafanaWebhookStep } from "@/components/grafana/GrafanaWebhookStep";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEYS = {
  STATUS: 'grafana_connection_status',
  WEBHOOK: 'grafana_webhook_url',
};

function setConnectedState(connected: boolean) {
  if (typeof window === "undefined") return;
  if (connected) {
    localStorage.setItem("isGrafanaConnected", "true");
  } else {
    localStorage.removeItem("isGrafanaConnected");
  }
  window.dispatchEvent(new CustomEvent("providerStateChanged"));
  window.dispatchEvent(new CustomEvent("providerPreferenceChanged", { detail: { providers: connected ? ["grafana"] : [] } }));
}

export default function GrafanaAuthPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<GrafanaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadWebhookUrl = async () => {
    try {
      const response = await grafanaService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEYS.WEBHOOK, response.webhookUrl);
      }
    } catch (err) {
      console.error("Failed to load webhook URL", err);
    }
  };

  const refreshStatus = useCallback(async () => {
    try {
      const result = await grafanaService.getStatus();
      setStatus(result);
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
      }
      if (result?.connected) {
        setConnectedState(true);
        await loadWebhookUrl();
      }
    } catch (err) {
      console.error("Failed to load Grafana status", err);
    }
  }, []);

  useEffect(() => {
    const cached = typeof window !== "undefined" ? localStorage.getItem(CACHE_KEYS.STATUS) : null;
    if (cached) {
      try {
        const parsed = JSON.parse(cached);
        setStatus(parsed);
        if (parsed?.connected) {
          const cachedWebhook = localStorage.getItem(CACHE_KEYS.WEBHOOK);
          if (cachedWebhook) setWebhookUrl(cachedWebhook);
        }
      } catch {}
    }
    refreshStatus();
  }, [refreshStatus]);

  const handleConnected = useCallback(async () => {
    await refreshStatus();
    setConnectedState(true);
    try {
      await fetch("/api/provider-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", provider: "grafana" }),
      });
    } catch {}
    toast({ title: "Connected", description: "Grafana connected via webhook." });
  }, [refreshStatus, toast]);

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/grafana", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setWebhookUrl(null);
        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEYS.STATUS);
          localStorage.removeItem(CACHE_KEYS.WEBHOOK);
        }
        setConnectedState(false);
        try {
          await fetch("/api/provider-preferences", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "remove", provider: "grafana" }),
          });
        } catch {}
        toast({ title: "Disconnected", description: "Grafana disconnected successfully" });
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Grafana");
      }
    } catch (err: any) {
      console.error("Grafana disconnect failed", err);
      toast({
        title: "Failed to disconnect Grafana",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhook = () => {
    if (webhookUrl) {
      copyToClipboard(webhookUrl);
      setCopied(true);
      toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <ConnectorAuthGuard connectorName="Grafana">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6 flex items-center gap-3">
          <img src="/grafana.svg" alt="Grafana" className="h-9 w-9" />
          <div>
            <h1 className="text-3xl font-bold">Grafana Integration</h1>
            <p className="text-muted-foreground mt-1">
              Connect your Grafana instance and configure alert webhooks
            </p>
          </div>
        </div>

        {!status?.connected ? (
          <GrafanaConnectionStep onConnected={handleConnected} />
        ) : status && webhookUrl ? (
          <GrafanaWebhookStep
            status={status}
            webhookUrl={webhookUrl}
            copied={copied}
            onCopy={handleCopyWebhook}
            onDisconnect={handleDisconnect}
            loading={loading}
          />
        ) : null}
      </div>
    </ConnectorAuthGuard>
  );
}

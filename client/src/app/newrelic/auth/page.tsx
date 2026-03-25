"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { newrelicService, NewRelicStatus } from "@/lib/services/newrelic";
import { NewRelicConnectionStep } from "@/components/newrelic/NewRelicConnectionStep";
import { NewRelicWebhookStep } from "@/components/newrelic/NewRelicWebhookStep";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEYS = {
  STATUS: 'newrelic_connection_status',
  WEBHOOK: 'newrelic_webhook_url',
};

export default function NewRelicAuthPage() {
  const { toast } = useToast();
  const [apiKey, setApiKey] = useState("");
  const [accountId, setAccountId] = useState("");
  const [region, setRegion] = useState("us");
  const [licenseKey, setLicenseKey] = useState("");
  const [status, setStatus] = useState<NewRelicStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === 'undefined') return;
    if (connected) {
      localStorage.setItem('isNewRelicConnected', 'true');
    } else {
      localStorage.removeItem('isNewRelicConnected');
    }
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
  };

  const loadWebhookUrl = async () => {
    try {
      const response = await newrelicService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.WEBHOOK, response.webhookUrl);
      }
    } catch (error: unknown) {
      console.error('[newrelic] Failed to load webhook URL', error);
    }
  };

  const fetchAndUpdateStatus = async () => {
    const result = await newrelicService.getStatus();
    setStatus(result);

    if (typeof window !== 'undefined' && result) {
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
    }

    updateLocalStorageConnection(result?.connected ?? false);

    if (result?.connected) {
      setRegion(result.region || "us");
      await loadWebhookUrl();
    } else if (typeof window !== 'undefined') {
      localStorage.removeItem(CACHE_KEYS.WEBHOOK);
    }
  };

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== 'undefined') {
        const cachedStatus = localStorage.getItem(CACHE_KEYS.STATUS);
        const cachedWebhook = localStorage.getItem(CACHE_KEYS.WEBHOOK);

        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus) as NewRelicStatus;
          setStatus(parsedStatus);
          updateLocalStorageConnection(parsedStatus?.connected ?? false);
          if (parsedStatus?.connected) {
            setRegion(parsedStatus.region || "us");
            if (cachedWebhook) setWebhookUrl(cachedWebhook);
          }

          if (isInitialLoad) {
            setIsInitialLoad(false);
            fetchAndUpdateStatus();
            return;
          }
          return;
        }
      }

      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error('[newrelic] Failed to load status', error);
      toast({ title: 'Error', description: 'Unable to load New Relic status', variant: 'destructive' });
    }
  };

  useEffect(() => {
    loadStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = {
        apiKey,
        accountId,
        region,
        licenseKey: licenseKey || undefined,
      };
      const result = await newrelicService.connect(payload);
      setStatus(result);

      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
        localStorage.setItem('isNewRelicConnected', 'true');
      }

      toast({
        title: 'Success',
        description: 'New Relic connected successfully. Configure the webhook below to start receiving alerts.',
      });

      await loadWebhookUrl();
      updateLocalStorageConnection(true);

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', provider: 'newrelic' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: ['newrelic'] } }));
      } catch (prefErr: unknown) {
        console.warn('[newrelic] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[newrelic] Connect failed', error);
      const message = getUserFriendlyError(error);
      toast({ title: 'Failed to connect to New Relic', description: message, variant: 'destructive' });
    } finally {
      setLoading(false);
      setApiKey('');
      setLicenseKey('');
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/connected-accounts/newrelic', {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || 'Failed to disconnect New Relic');
      }

      setStatus({ connected: false });
      setWebhookUrl(null);
      setAccountId('');
      setRegion("us");

      if (typeof window !== 'undefined') {
        localStorage.removeItem(CACHE_KEYS.STATUS);
        localStorage.removeItem(CACHE_KEYS.WEBHOOK);
        localStorage.removeItem('isNewRelicConnected');
      }

      updateLocalStorageConnection(false);
      toast({ title: 'Success', description: 'New Relic disconnected successfully.' });

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', provider: 'newrelic' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: [] } }));
      } catch (prefErr: unknown) {
        console.warn('[newrelic] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[newrelic] Disconnect failed', error);
      const message = getUserFriendlyError(error);
      toast({ title: 'Failed to disconnect New Relic', description: message, variant: 'destructive' });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhook = () => {
    if (!webhookUrl) return;
    copyToClipboard(webhookUrl);
    setCopied(true);
    toast({ title: 'Copied', description: 'Webhook URL copied to clipboard' });
    setTimeout(() => setCopied(false), 2000);
  };

  const isConnected = Boolean(status?.connected);

  return (
    <ConnectorAuthGuard connectorName="New Relic">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">New Relic Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect New Relic to query metrics, logs, traces, and alert issues via NerdGraph for root cause analysis.
          </p>
        </div>

        <div className="flex items-center justify-center mb-8">
          <div className="flex items-center">
            <div className={`flex items-center justify-center w-10 h-10 rounded-full ${!isConnected ? 'bg-[#00AC69] text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
              1
            </div>
            <div className={`w-24 h-1 ${isConnected ? 'bg-[#00AC69]' : 'bg-gray-200'}`}></div>
            <div className={`flex items-center justify-center w-10 h-10 rounded-full ${isConnected ? 'bg-[#00AC69] text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
              2
            </div>
          </div>
        </div>

        <div className="flex items-center justify-center mb-6 text-sm font-medium">
          <span className={!isConnected ? 'text-[#00AC69]' : 'text-muted-foreground'}>
            Connect New Relic
          </span>
          <span className="mx-4 text-muted-foreground">&rarr;</span>
          <span className={isConnected ? 'text-[#00AC69]' : 'text-muted-foreground'}>
            Configure Webhook
          </span>
        </div>

        {!isConnected ? (
          <NewRelicConnectionStep
            apiKey={apiKey}
            setApiKey={setApiKey}
            accountId={accountId}
            setAccountId={setAccountId}
            region={region}
            setRegion={setRegion}
            licenseKey={licenseKey}
            setLicenseKey={setLicenseKey}
            loading={loading}
            onConnect={handleConnect}
          />
        ) : status && webhookUrl ? (
          <NewRelicWebhookStep
            status={status}
            webhookUrl={webhookUrl}
            copied={copied}
            onCopy={handleCopyWebhook}
            onDisconnect={handleDisconnect}
            loading={loading}
          />
        ) : isConnected ? (
          <div className="flex items-center justify-center py-12">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#00AC69]" />
            <span className="ml-3 text-muted-foreground">Loading webhook configuration…</span>
          </div>
        ) : null}
      </div>
    </ConnectorAuthGuard>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { datadogService, DatadogStatus } from "@/lib/services/datadog";
import { DatadogConnectionStep } from "@/components/datadog/DatadogConnectionStep";
import { DatadogWebhookStep } from "@/components/datadog/DatadogWebhookStep";

const CACHE_KEYS = {
  STATUS: 'datadog_connection_status',
  WEBHOOK: 'datadog_webhook_url',
};

const DEFAULT_SITE = 'datadoghq.com';

const getUserFriendlyError = (err: unknown): string => {
  if (!err) {
    return "An unexpected error occurred. Please try again.";
  }

  if (typeof err === 'string') {
    return err;
  }

  if (err instanceof Error) {
    const { message } = err;
    if (!message) {
      return 'An unexpected error occurred. Please try again.';
    }
    try {
      const parsed = JSON.parse(message) as { error?: string };
      return parsed.error ?? message;
    } catch {
      return message;
    }
  }

  if (typeof err === 'object') {
    const errorValue = (err as Record<string, unknown>).error;
    if (typeof errorValue === 'string') {
      return errorValue;
    }
  }

  return 'An unexpected error occurred. Please try again.';
};

export default function DatadogAuthPage() {
  const { toast } = useToast();
  const [apiKey, setApiKey] = useState("");
  const [appKey, setAppKey] = useState("");
  const [site, setSite] = useState(DEFAULT_SITE);
  const [serviceAccountName, setServiceAccountName] = useState("");
  const [status, setStatus] = useState<DatadogStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === 'undefined') return;

    if (connected) {
      localStorage.setItem('isDatadogConnected', 'true');
    } else {
      localStorage.removeItem('isDatadogConnected');
    }
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
  };

  const loadWebhookUrl = async () => {
    try {
      const response = await datadogService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.WEBHOOK, response.webhookUrl);
      }
    } catch (error: unknown) {
      console.error('[datadog] Failed to load webhook URL', error);
    }
  };

  const fetchAndUpdateStatus = async () => {
    const result = await datadogService.getStatus();
    setStatus(result);

    if (typeof window !== 'undefined' && result) {
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
    }

    // Update the isDatadogConnected localStorage flag based on the actual connection status
    updateLocalStorageConnection(result?.connected ?? false);

    if (result?.connected) {
      setSite(result.site || DEFAULT_SITE);
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
          const parsedStatus = JSON.parse(cachedStatus) as DatadogStatus;
          setStatus(parsedStatus);
          // Update localStorage flag based on cached status
          updateLocalStorageConnection(parsedStatus?.connected ?? false);
          if (parsedStatus?.connected) {
            setSite(parsedStatus.site || DEFAULT_SITE);
            if (cachedWebhook) {
              setWebhookUrl(cachedWebhook);
            }
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
      console.error('[datadog] Failed to load status', error);
      toast({
        title: 'Error',
        description: 'Unable to load Datadog status',
        variant: 'destructive',
      });
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = { apiKey, appKey, site, serviceAccountName: serviceAccountName || undefined };
      const result = await datadogService.connect(payload);
      setStatus(result);

      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
        localStorage.setItem('isDatadogConnected', 'true');
      }

      toast({
        title: 'Success',
        description: 'Datadog connected successfully. Configure the webhook below to start receiving alerts.',
      });

      await loadWebhookUrl();
      updateLocalStorageConnection(true);

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', provider: 'datadog' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: ['datadog'] } }));
      } catch (prefErr: unknown) {
        console.warn('[datadog] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[datadog] Connect failed', error);
      const message = getUserFriendlyError(error);
      toast({
        title: 'Failed to connect to Datadog',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
      setApiKey('');
      setAppKey('');
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);

    try {
      const response = await fetch('/api/connected-accounts/datadog', {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || 'Failed to disconnect Datadog');
      }

      setStatus({ connected: false });
      setWebhookUrl(null);
      setServiceAccountName('');
      setSite(DEFAULT_SITE);

      if (typeof window !== 'undefined') {
        localStorage.removeItem(CACHE_KEYS.STATUS);
        localStorage.removeItem(CACHE_KEYS.WEBHOOK);
        localStorage.removeItem('isDatadogConnected');
      }

      updateLocalStorageConnection(false);

      toast({
        title: 'Success',
        description: 'Datadog disconnected successfully.',
      });

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', provider: 'datadog' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: [] } }));
      } catch (prefErr: unknown) {
        console.warn('[datadog] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[datadog] Disconnect failed', error);
      const message = getUserFriendlyError(error);
      toast({
        title: 'Failed to disconnect Datadog',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhook = () => {
    if (!webhookUrl) return;
    navigator.clipboard.writeText(webhookUrl);
    setCopied(true);
    toast({ title: 'Copied', description: 'Webhook URL copied to clipboard' });
    setTimeout(() => setCopied(false), 2000);
  };

  const isConnected = Boolean(status?.connected);

  return (
    <div className="container mx-auto py-8 px-4 max-w-5xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Datadog Integration</h1>
        <p className="text-muted-foreground mt-1">
          Securely connect Datadog to ingest logs, metrics, monitors, and alerts inside Aurora.
        </p>
      </div>

      <div className="flex items-center justify-center mb-8">
        <div className="flex items-center">
          <div className={`flex items-center justify-center w-10 h-10 rounded-full ${!isConnected ? 'bg-purple-600 text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
            1
          </div>
          <div className={`w-24 h-1 ${isConnected ? 'bg-purple-600' : 'bg-gray-200'}`}></div>
          <div className={`flex items-center justify-center w-10 h-10 rounded-full ${isConnected ? 'bg-purple-600 text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
            2
          </div>
        </div>
      </div>

      <div className="flex items-center justify-center mb-6 text-sm font-medium">
        <span className={!isConnected ? 'text-purple-600' : 'text-muted-foreground'}>
          Connect Datadog
        </span>
        <span className="mx-4 text-muted-foreground">→</span>
        <span className={isConnected ? 'text-purple-600' : 'text-muted-foreground'}>
          Configure Webhook
        </span>
      </div>

      {!isConnected ? (
        <DatadogConnectionStep
          apiKey={apiKey}
          setApiKey={setApiKey}
          appKey={appKey}
          setAppKey={setAppKey}
          site={site}
          setSite={setSite}
          serviceAccountName={serviceAccountName}
          setServiceAccountName={setServiceAccountName}
          loading={loading}
          onConnect={handleConnect}
        />
      ) : status && webhookUrl ? (
        <DatadogWebhookStep
          status={status}
          webhookUrl={webhookUrl}
          copied={copied}
          onCopy={handleCopyWebhook}
          onDisconnect={handleDisconnect}
          loading={loading}
        />
      ) : null}
    </div>
  );
}

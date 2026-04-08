"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { opsgenieService, OpsGenieStatus } from "@/lib/services/opsgenie";
import { OpsGenieConnectionStep } from "@/components/opsgenie/OpsGenieConnectionStep";
import { OpsGenieWebhookStep } from "@/components/opsgenie/OpsGenieWebhookStep";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEYS = {
  STATUS: 'opsgenie_connection_status',
  WEBHOOK: 'opsgenie_webhook_url',
};

const DEFAULT_REGION = 'us';

export default function OpsGenieAuthPage() {
  const { toast } = useToast();
  const [apiKey, setApiKey] = useState("");
  const [region, setRegion] = useState(DEFAULT_REGION);
  const [authType, setAuthType] = useState<"opsgenie" | "jsm">("opsgenie");
  const [jsmEmail, setJsmEmail] = useState("");
  const [jsmApiToken, setJsmApiToken] = useState("");
  const [jsmSiteUrl, setJsmSiteUrl] = useState("");
  const [status, setStatus] = useState<OpsGenieStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === 'undefined') return;

    if (connected) {
      localStorage.setItem('isOpsGenieConnected', 'true');
    } else {
      localStorage.removeItem('isOpsGenieConnected');
    }
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
  };

  const loadWebhookUrl = async () => {
    try {
      const response = await opsgenieService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.WEBHOOK, response.webhookUrl);
      }
    } catch (error: unknown) {
      console.error('[opsgenie] Failed to load webhook URL', error);
      toast({ title: 'Warning', description: 'Failed to load webhook URL. Check server configuration.', variant: 'destructive' });
    }
  };

  const fetchAndUpdateStatus = async () => {
    const result = await opsgenieService.getStatus();
    setStatus(result);

    if (typeof window !== 'undefined' && result) {
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
    }

    updateLocalStorageConnection(result?.connected ?? false);

    if (result?.connected) {
      setRegion(result.region || DEFAULT_REGION);
      if (result.authType === 'jsm_basic') {
        setAuthType('jsm');
      }
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
          let parsedStatus: OpsGenieStatus;
          try {
            parsedStatus = JSON.parse(cachedStatus) as OpsGenieStatus;
          } catch {
            localStorage.removeItem(CACHE_KEYS.STATUS);
            return;
          }
          setStatus(parsedStatus);
          updateLocalStorageConnection(parsedStatus?.connected ?? false);
          if (parsedStatus?.connected) {
            setRegion(parsedStatus.region || DEFAULT_REGION);
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
      console.error('[opsgenie] Failed to load status', error);
      toast({
        title: 'Error',
        description: 'Unable to load OpsGenie status',
        variant: 'destructive',
      });
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const isJSM = authType === 'jsm';

    if (isJSM) {
      if (!jsmEmail.trim() || !jsmApiToken.trim() || !jsmSiteUrl.trim()) {
        toast({
          title: 'Validation error',
          description: 'Email, API token, and site URL are all required.',
          variant: 'destructive',
        });
        return;
      }
    } else if (!apiKey.trim()) {
      toast({
        title: 'Validation error',
        description: 'API key is required.',
        variant: 'destructive',
      });
      return;
    }

    setLoading(true);

    try {
      const connectPayload = isJSM
        ? { authType: 'jsm', email: jsmEmail, apiToken: jsmApiToken, siteUrl: jsmSiteUrl }
        : { apiKey, region };
      const result = await opsgenieService.connect(connectPayload);
      setStatus(result);

      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
      }

      toast({
        title: 'Success',
        description: isJSM
          ? 'JSM Operations connected successfully. Configure the webhook below to start receiving alerts.'
          : 'OpsGenie connected successfully. Configure the webhook below to start receiving alerts.',
      });

      updateLocalStorageConnection(true);
      await loadWebhookUrl();

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', provider: 'opsgenie' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: ['opsgenie'] } }));
      } catch (prefErr: unknown) {
        console.warn('[opsgenie] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[opsgenie] Connect failed', error);
      const message = getUserFriendlyError(error);
      toast({
        title: 'Failed to connect to OpsGenie',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
      setApiKey('');
      setJsmApiToken('');
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);

    try {
      const response = await fetch('/api/connected-accounts/opsgenie', {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || 'Failed to disconnect OpsGenie');
      }

      setStatus({ connected: false });
      setWebhookUrl(null);
      setRegion(DEFAULT_REGION);

      if (typeof window !== 'undefined') {
        localStorage.removeItem(CACHE_KEYS.STATUS);
        localStorage.removeItem(CACHE_KEYS.WEBHOOK);
      }

      updateLocalStorageConnection(false);

      toast({
        title: 'Success',
        description: 'OpsGenie disconnected successfully.',
      });

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', provider: 'opsgenie' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: [] } }));
      } catch (prefErr: unknown) {
        console.warn('[opsgenie] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[opsgenie] Disconnect failed', error);
      const message = getUserFriendlyError(error);
      toast({
        title: 'Failed to disconnect OpsGenie',
        description: message,
        variant: 'destructive',
      });
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
    <ConnectorAuthGuard connectorName="OpsGenie">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">
            {authType === 'jsm' ? 'JSM Operations Integration' : 'OpsGenie Integration'}
          </h1>
          <p className="text-muted-foreground mt-1">
            {authType === 'jsm'
              ? 'Securely connect JSM Operations to ingest alerts and on-call schedules inside Aurora.'
              : 'Securely connect OpsGenie to ingest alerts, incidents, and on-call schedules inside Aurora.'}
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
            {authType === 'jsm' ? 'Connect JSM Operations' : 'Connect OpsGenie'}
          </span>
          <span className="mx-4 text-muted-foreground">&rarr;</span>
          <span className={isConnected ? 'text-purple-600' : 'text-muted-foreground'}>
            Configure Webhook
          </span>
        </div>

        {!isConnected ? (
          <OpsGenieConnectionStep
            apiKey={apiKey}
            setApiKey={setApiKey}
            region={region}
            setRegion={setRegion}
            authType={authType}
            setAuthType={setAuthType}
            jsmEmail={jsmEmail}
            setJsmEmail={setJsmEmail}
            jsmApiToken={jsmApiToken}
            setJsmApiToken={setJsmApiToken}
            jsmSiteUrl={jsmSiteUrl}
            setJsmSiteUrl={setJsmSiteUrl}
            loading={loading}
            onConnect={handleConnect}
          />
        ) : status && webhookUrl ? (
          <OpsGenieWebhookStep
            status={status}
            webhookUrl={webhookUrl}
            copied={copied}
            onCopy={handleCopyWebhook}
            onDisconnect={handleDisconnect}
            loading={loading}
            authType={authType}
          />
        ) : null}
      </div>
    </ConnectorAuthGuard>
  );
}

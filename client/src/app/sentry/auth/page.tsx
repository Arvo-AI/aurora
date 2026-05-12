"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { sentryService, SentryStatus } from "@/lib/services/sentry";
import { SentryConnectionStep } from "@/components/sentry/SentryConnectionStep";
import { SentryWebhookStep } from "@/components/sentry/SentryWebhookStep";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { SENTRY_PURPLE } from "@/components/sentry/constants";

const CACHE_KEYS = {
  STATUS: 'sentry_connection_status',
};

type CachedStatus = Pick<SentryStatus, 'connected' | 'region' | 'orgSlug' | 'hasWebhookSecret'>;

export default function SentryAuthPage() {
  const { toast } = useToast();
  const [authToken, setAuthToken] = useState("");
  const [orgSlug, setOrgSlug] = useState("");
  const [region, setRegion] = useState("us");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [status, setStatus] = useState<SentryStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === 'undefined') return;
    if (connected) {
      localStorage.setItem('isSentryConnected', 'true');
    } else {
      localStorage.removeItem('isSentryConnected');
    }
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
    window.dispatchEvent(new Event('sentryStateChanged'));
  };

  const loadWebhookUrl = async () => {
    try {
      const response = await sentryService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
    } catch (error: unknown) {
      console.error('[sentry] Failed to load webhook URL', error);
    }
  };

  const fetchAndUpdateStatus = async () => {
    const result = await sentryService.getStatus();
    setStatus(result);

    if (typeof window !== 'undefined' && result) {
      const cached: CachedStatus = {
        connected: result.connected,
        region: result.region,
        orgSlug: result.orgSlug,
        hasWebhookSecret: result.hasWebhookSecret,
      };
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(cached));
    }

    updateLocalStorageConnection(result?.connected ?? false);

    if (result?.connected) {
      setRegion(result.region || "us");
      if (result.orgSlug) setOrgSlug(result.orgSlug);
      await loadWebhookUrl();
    } else if (typeof window !== 'undefined') {
      localStorage.removeItem(CACHE_KEYS.STATUS);
    }
  };

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== 'undefined') {
        const cachedStatus = localStorage.getItem(CACHE_KEYS.STATUS);

        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus) as CachedStatus;
          setStatus(parsedStatus);
          updateLocalStorageConnection(parsedStatus?.connected ?? false);
          if (parsedStatus?.connected) {
            setRegion(parsedStatus.region || "us");
            if (parsedStatus.orgSlug) setOrgSlug(parsedStatus.orgSlug);
          }

          // Show cache immediately, then revalidate in the background
          // so a stale connection state corrects itself on the next tick.
          fetchAndUpdateStatus();
          return;
        }
      }

      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error('[sentry] Failed to load status', error);
      toast({ title: 'Error', description: 'Unable to load Sentry status', variant: 'destructive' });
    }
  };

  useEffect(() => {
    loadStatus();
    loadWebhookUrl();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = {
        authToken,
        orgSlug,
        region,
        webhookSecret,
      };
      const result = await sentryService.connect(payload);
      setStatus(result);

      if (typeof window !== 'undefined') {
        const cached: CachedStatus = {
          connected: true,
          region: result.region,
          orgSlug: result.orgSlug,
          hasWebhookSecret: result.hasWebhookSecret,
        };
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(cached));
        localStorage.setItem('isSentryConnected', 'true');
      }

      toast({
        title: 'Success',
        description: 'Sentry connected. Verify the webhook URL is set in your Sentry Internal Integration below.',
      });

      await loadWebhookUrl();
      updateLocalStorageConnection(true);

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', provider: 'sentry' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: ['sentry'] } }));
      } catch (prefErr: unknown) {
        console.warn('[sentry] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[sentry] Connect failed', error);
      const message = getUserFriendlyError(error);
      toast({ title: 'Failed to connect to Sentry', description: message, variant: 'destructive' });
    } finally {
      setLoading(false);
      setAuthToken('');
      setWebhookSecret('');
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/connected-accounts/sentry', {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || 'Failed to disconnect Sentry');
      }

      setStatus({ connected: false });
      setWebhookUrl(null);
      setOrgSlug('');
      setRegion("us");

      if (typeof window !== 'undefined') {
        localStorage.removeItem(CACHE_KEYS.STATUS);
        localStorage.removeItem('isSentryConnected');
      }

      updateLocalStorageConnection(false);
      toast({ title: 'Success', description: 'Sentry disconnected successfully.' });

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', provider: 'sentry' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: [] } }));
      } catch (prefErr: unknown) {
        console.warn('[sentry] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[sentry] Disconnect failed', error);
      const message = getUserFriendlyError(error);
      toast({ title: 'Failed to disconnect Sentry', description: message, variant: 'destructive' });
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
    <ConnectorAuthGuard connectorName="Sentry">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Sentry Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect Sentry to ingest issue and error webhooks and query full stacktraces for automated root cause analysis.
          </p>
        </div>

        <div className="flex items-center justify-center mb-8">
          <div className="flex items-center">
            <div
              className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${!isConnected ? 'text-white' : 'bg-gray-200 text-gray-600'}`}
              style={!isConnected ? { backgroundColor: SENTRY_PURPLE } : undefined}
            >
              1
            </div>
            <div className="w-24 h-1" style={{ backgroundColor: isConnected ? SENTRY_PURPLE : '#e5e7eb' }}></div>
            <div
              className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${isConnected ? 'text-white' : 'bg-gray-200 text-gray-600'}`}
              style={isConnected ? { backgroundColor: SENTRY_PURPLE } : undefined}
            >
              2
            </div>
          </div>
        </div>

        <div className="flex items-center justify-center mb-6 text-sm font-medium">
          <span style={{ color: !isConnected ? SENTRY_PURPLE : undefined }} className={!isConnected ? undefined : 'text-muted-foreground'}>
            Connect Sentry
          </span>
          <span className="mx-4 text-muted-foreground">&rarr;</span>
          <span style={{ color: isConnected ? SENTRY_PURPLE : undefined }} className={isConnected ? undefined : 'text-muted-foreground'}>
            Verify Webhook
          </span>
        </div>

        {!isConnected ? (
          <SentryConnectionStep
            authToken={authToken}
            setAuthToken={setAuthToken}
            orgSlug={orgSlug}
            setOrgSlug={setOrgSlug}
            region={region}
            setRegion={setRegion}
            webhookSecret={webhookSecret}
            setWebhookSecret={setWebhookSecret}
            loading={loading}
            onConnect={handleConnect}
            webhookUrl={webhookUrl}
            copied={copied}
            onCopyWebhook={handleCopyWebhook}
          />
        ) : status && webhookUrl ? (
          <SentryWebhookStep
            status={status}
            webhookUrl={webhookUrl}
            copied={copied}
            onCopy={handleCopyWebhook}
            onDisconnect={handleDisconnect}
            loading={loading}
          />
        ) : isConnected ? (
          <div className="flex items-center justify-center py-12">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2" style={{ borderColor: SENTRY_PURPLE }} />
            <span className="ml-3 text-muted-foreground">Loading webhook configuration…</span>
          </div>
        ) : null}
      </div>
    </ConnectorAuthGuard>
  );
}

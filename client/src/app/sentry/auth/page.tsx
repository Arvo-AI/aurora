"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { sentryService, SentryStatus } from "@/lib/services/sentry";
import { providerPreferencesService } from "@/lib/services/providerPreferences";
import { SentryConnectionStep } from "@/components/sentry/SentryConnectionStep";
import { SentryWebhookStep } from "@/components/sentry/SentryWebhookStep";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { SENTRY_PURPLE } from "@/components/sentry/constants";

const PROVIDER_ID = 'sentry';

function broadcastStateChange() {
  if (globalThis.window === undefined) return;
  globalThis.window.dispatchEvent(new CustomEvent('providerStateChanged'));
  globalThis.window.dispatchEvent(new Event('sentryStateChanged'));
}

function broadcastPreferenceChange(providers: string[]) {
  if (globalThis.window === undefined) return;
  globalThis.window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers } }));
}

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

  const applyStatus = (next: SentryStatus | null) => {
    setStatus(next);
    if (next) {
      sentryService.cacheStatus(next);
      if (next.connected) {
        setRegion(next.region || "us");
        if (next.orgSlug) setOrgSlug(next.orgSlug);
      }
    } else {
      sentryService.clearCachedStatus();
    }
    broadcastStateChange();
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
    applyStatus(result);
    if (result?.connected) await loadWebhookUrl();
  };

  const loadStatus = async () => {
    try {
      const cached = sentryService.loadCachedStatus();
      if (cached) {
        applyStatus({ ...cached });
        // Background revalidate so a stale cached state self-corrects.
        fetchAndUpdateStatus();
        return;
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
      const result = await sentryService.connect({ authToken, orgSlug, region, webhookSecret });
      applyStatus(result);
      toast({
        title: 'Success',
        description: 'Sentry connected. Verify the webhook URL is set in your Sentry Internal Integration below.',
      });
      await loadWebhookUrl();
      // Notify listeners regardless of the preference write — the connection
      // already succeeded above, and a transient failure on the preference sync
      // shouldn't leave the rest of the app stuck on the previous state.
      providerPreferencesService.addProvider(PROVIDER_ID).catch((prefErr) => {
        console.warn('[sentry] Failed to add provider preference', prefErr);
      });
      broadcastPreferenceChange([PROVIDER_ID]);
    } catch (error: unknown) {
      console.error('[sentry] Connect failed', error);
      toast({ title: 'Failed to connect to Sentry', description: getUserFriendlyError(error), variant: 'destructive' });
    } finally {
      setLoading(false);
      setAuthToken('');
      setWebhookSecret('');
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/connected-accounts/sentry', { method: 'DELETE', credentials: 'include' });
      if (!response.ok && response.status !== 204) {
        throw new Error((await response.text()) || 'Failed to disconnect Sentry');
      }
      applyStatus({ connected: false });
      setWebhookUrl(null);
      setOrgSlug('');
      setRegion("us");
      toast({ title: 'Success', description: 'Sentry disconnected successfully.' });
      // The DELETE above already succeeded — surface the state change to other
      // components regardless of whether the preference-sync POST succeeds.
      providerPreferencesService.removeProvider(PROVIDER_ID).catch((prefErr) => {
        console.warn('[sentry] Failed to remove provider preference', prefErr);
      });
      broadcastPreferenceChange([]);
    } catch (error: unknown) {
      console.error('[sentry] Disconnect failed', error);
      toast({ title: 'Failed to disconnect Sentry', description: getUserFriendlyError(error), variant: 'destructive' });
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

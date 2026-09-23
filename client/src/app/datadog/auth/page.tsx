"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { datadogService, DatadogStatus } from "@/lib/services/datadog";
import { DatadogConnectionStep } from "@/components/datadog/DatadogConnectionStep";
import { DatadogWebhookStep } from "@/components/datadog/DatadogWebhookStep";
import { DatadogAccountList } from "@/components/datadog/DatadogAccountList";
import { Button } from "@/components/ui/button";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEYS = {
  STATUS: 'datadog_connection_status',
  WEBHOOK: 'datadog_webhook_url',
};

const DEFAULT_SITE = 'datadoghq.com';

export default function DatadogAuthPage() {
  const { toast } = useToast();
  const [apiKey, setApiKey] = useState("");
  const [appKey, setAppKey] = useState("");
  const [site, setSite] = useState(DEFAULT_SITE);
  const [serviceAccountName, setServiceAccountName] = useState("");
  const [label, setLabel] = useState("");
  const [status, setStatus] = useState<DatadogStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);
  const [showAddForm, setShowAddForm] = useState(false);
  const [removingLabel, setRemovingLabel] = useState<string | null>(null);
  const [connectError, setConnectError] = useState<string | null>(null);

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
      // Only seed the form's site from status when not adding another org, or
      // typing a second org's site would be overwritten by the primary's.
      if (!showAddForm) {
        setSite(result.site || DEFAULT_SITE);
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
          const parsedStatus = JSON.parse(cachedStatus) as DatadogStatus;
          // A cache written before multi-org support has no accounts array; the
          // refetch below fills it in rather than rendering an empty org list.
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

          if (!parsedStatus?.accounts) {
            await fetchAndUpdateStatus();
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
    let conflict = false;
    setConnectError(null);

    try {
      const payload = {
        apiKey,
        appKey,
        site,
        serviceAccountName: serviceAccountName || undefined,
        label: label.trim() || undefined,
      };
      const result = await datadogService.connect(payload);
      setStatus(result);
      setShowAddForm(false);

      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
        localStorage.setItem('isDatadogConnected', 'true');
      }

      const orgCount = result.accounts?.length ?? 1;
      toast({
        title: 'Success',
        description: result.replaced
          ? `Credentials for "${result.label}" updated.`
          : orgCount > 1
            ? `Datadog organization "${result.label}" connected. ${orgCount} organizations are now connected - make sure the webhook below exists in each one.`
            : 'Datadog connected successfully. Configure the webhook below to start receiving alerts.',
      });

      await loadWebhookUrl();
      updateLocalStorageConnection(true);
      // Re-fetch so the account list carries per-org validity, which /connect
      // does not report for the orgs it did not just validate.
      await fetchAndUpdateStatus();

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
      // 409 means the label is taken by a different organization. It is fixable
      // by typing another label, so show it inline on the form and keep the keys
      // rather than firing a toast that vanishes and clearing the inputs.
      conflict = (error as { status?: number })?.status === 409;
      if (conflict) {
        setConnectError(message);
      } else {
        toast({
          title: 'Failed to connect to Datadog',
          description: message,
          variant: 'destructive',
        });
      }
    } finally {
      setLoading(false);
      if (!conflict) {
        setApiKey('');
        setAppKey('');
        setLabel('');
        setServiceAccountName('');
      }
    }
  };

  const handleRemoveAccount = async (accountLabel: string) => {
    setRemovingLabel(accountLabel);

    try {
      const { accounts } = await datadogService.disconnect(accountLabel);
      toast({
        title: 'Removed',
        description: `Datadog organization "${accountLabel}" removed.`,
      });

      if (accounts.length === 0) {
        // That was the last org, so the backend tore the whole provider down.
        await resetLocalConnectionState();
        return;
      }

      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error('[datadog] Remove account failed', error);
      toast({
        title: 'Failed to remove organization',
        description: getUserFriendlyError(error),
        variant: 'destructive',
      });
    } finally {
      setRemovingLabel(null);
    }
  };

  const resetLocalConnectionState = async () => {
    setStatus({ connected: false, accounts: [] });
    setWebhookUrl(null);
    setServiceAccountName('');
    setLabel('');
    setSite(DEFAULT_SITE);
    setShowAddForm(false);

    if (typeof window !== 'undefined') {
      localStorage.removeItem(CACHE_KEYS.STATUS);
      localStorage.removeItem(CACHE_KEYS.WEBHOOK);
      localStorage.removeItem('isDatadogConnected');
    }

    updateLocalStorageConnection(false);

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
  };

  const handleDisconnect = async () => {
    setLoading(true);

    try {
      // No account selector: removes every connected organization.
      await datadogService.disconnect();

      await resetLocalConnectionState();

      toast({
        title: 'Success',
        description: 'Datadog disconnected successfully.',
      });
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
    copyToClipboard(webhookUrl);
    setCopied(true);
    toast({ title: 'Copied', description: 'Webhook URL copied to clipboard' });
    setTimeout(() => setCopied(false), 2000);
  };

  const isConnected = Boolean(status?.connected);
  // Fall back to a synthetic single entry so a legacy status payload (no accounts
  // array) still renders one row rather than an empty list.
  const accounts = status?.accounts?.length
    ? status.accounts
    : isConnected
      ? [{
          label: status?.label || status?.site || 'default',
          site: status?.site,
          orgName: (status?.org?.name as string | undefined) ?? null,
          serviceAccountName: status?.serviceAccountName ?? null,
          validatedAt: status?.validatedAt ?? null,
          valid: true,
        }]
      : [];

  return (
    <ConnectorAuthGuard connectorName="Datadog">
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
            label={label}
            setLabel={setLabel}
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
          >
            <div className="space-y-4">
              <DatadogAccountList
                accounts={accounts}
                onRemove={handleRemoveAccount}
                removingLabel={removingLabel}
                disabled={loading}
              />

              {showAddForm ? (
                <div className="space-y-3">
                  <DatadogConnectionStep
                    apiKey={apiKey}
                    setApiKey={setApiKey}
                    appKey={appKey}
                    setAppKey={setAppKey}
                    site={site}
                    setSite={setSite}
                    serviceAccountName={serviceAccountName}
                    setServiceAccountName={setServiceAccountName}
                    label={label}
                    setLabel={setLabel}
                    loading={loading}
                    onConnect={handleConnect}
                    isAdditional
                    error={connectError}
                  />
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => { setShowAddForm(false); setConnectError(null); }}
                    disabled={loading}
                  >
                    Cancel
                  </Button>
                </div>
              ) : (
                <Button variant="outline" size="sm" onClick={() => setShowAddForm(true)}>
                  Add another organization
                </Button>
              )}
            </div>
          </DatadogWebhookStep>
        ) : null}
      </div>
    </ConnectorAuthGuard>
  );
}

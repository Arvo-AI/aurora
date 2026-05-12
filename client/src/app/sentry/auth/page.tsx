"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { sentryService, SentryStatus, SentryIngestedEvent } from "@/lib/services/sentry";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Copy, Check, Loader2 } from "lucide-react";

const CACHE_KEYS = {
  STATUS: 'sentry_connection_status',
  WEBHOOK: 'sentry_webhook_url',
};

export default function SentryAuthPage() {
  const { toast } = useToast();
  const [authToken, setAuthToken] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [orgSlug, setOrgSlug] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [status, setStatus] = useState<SentryStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);
  const [events, setEvents] = useState<SentryIngestedEvent[]>([]);
  const [eventsTotal, setEventsTotal] = useState(0);
  const [eventsOffset, setEventsOffset] = useState(0);
  const [eventsLoading, setEventsLoading] = useState(false);
  const eventsLimit = 20;

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === 'undefined') return;

    if (connected) {
      localStorage.setItem('isSentryConnected', 'true');
    } else {
      localStorage.removeItem('isSentryConnected');
    }
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
  };

  const loadWebhookUrl = async () => {
    try {
      const response = await sentryService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.WEBHOOK, response.webhookUrl);
      }
    } catch (error: unknown) {
      console.error('[sentry] Failed to load webhook URL', error);
    }
  };

  const loadEvents = async (newOffset = 0) => {
    try {
      setEventsLoading(true);
      const params = new URLSearchParams({ limit: String(eventsLimit), offset: String(newOffset) });
      const response = await sentryService.getIngestedEvents(params);
      setEvents(response.events);
      setEventsTotal(response.total);
      setEventsOffset(newOffset);
    } catch (error: unknown) {
      console.error('[sentry] Failed to load events', error);
    } finally {
      setEventsLoading(false);
    }
  };

  const fetchAndUpdateStatus = async () => {
    const result = await sentryService.getStatus();
    setStatus(result);

    if (typeof window !== 'undefined' && result) {
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
    }

    updateLocalStorageConnection(result?.connected ?? false);

    if (result?.connected) {
      setBaseUrl(result.baseUrl || '');
      loadEvents();
    }
  };

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== 'undefined') {
        const cachedStatus = localStorage.getItem(CACHE_KEYS.STATUS);
        const cachedWebhook = localStorage.getItem(CACHE_KEYS.WEBHOOK);

        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus) as SentryStatus;
          setStatus(parsedStatus);
          updateLocalStorageConnection(parsedStatus?.connected ?? false);
          if (parsedStatus?.connected) {
            setBaseUrl(parsedStatus.baseUrl || '');
          }
          if (cachedWebhook) {
            setWebhookUrl(cachedWebhook);
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
      console.error('[sentry] Failed to load status', error);
      toast({
        title: 'Error',
        description: 'Unable to load Sentry status',
        variant: 'destructive',
      });
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
        clientSecret,
        orgSlug,
        baseUrl: baseUrl || undefined,
      };
      const result = await sentryService.connect(payload);
      setStatus(result);

      if (typeof window !== 'undefined') {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
        localStorage.setItem('isSentryConnected', 'true');
      }

      toast({
        title: 'Success',
        description: 'Sentry connected successfully.',
      });

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
      toast({
        title: 'Failed to connect to Sentry',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
      setAuthToken('');
      setClientSecret('');
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
      setBaseUrl('');

      if (typeof window !== 'undefined') {
        localStorage.removeItem(CACHE_KEYS.STATUS);
        localStorage.removeItem(CACHE_KEYS.WEBHOOK);
        localStorage.removeItem('isSentryConnected');
      }

      updateLocalStorageConnection(false);

      toast({
        title: 'Success',
        description: 'Sentry disconnected successfully.',
      });

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
      toast({
        title: 'Failed to disconnect Sentry',
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

  const getEventBadgeColor = (eventStatus: string) => {
    const lower = eventStatus.toLowerCase();
    if (lower === 'created' || lower === 'triggered' || lower === 'critical') return 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200';
    if (lower === 'resolved') return 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200';
    if (lower === 'warning') return 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200';
    if (lower === 'archived' || lower === 'ignored') return 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200';
    return 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200';
  };

  const formatDate = (dateStr: string) => {
    try {
      return new Date(dateStr).toLocaleString();
    } catch {
      return dateStr;
    }
  };

  return (
    <ConnectorAuthGuard connectorName="Sentry">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Sentry Integration</h1>
          <p className="text-muted-foreground mt-1">
            Securely connect Sentry to ingest errors, issues, and alerts inside Aurora.
          </p>
        </div>

        {!isConnected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect to Sentry</CardTitle>
              <CardDescription>
                Create an Internal Integration in Sentry to give Aurora API access and receive alerts.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="p-4 border rounded-lg bg-muted/30 space-y-4">
                <p className="text-sm font-medium">Setup Instructions:</p>

                <ol className="list-none space-y-2 text-sm text-muted-foreground">
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">1.</span>
                    <span>In Sentry, go to <code className="px-1.5 py-0.5 bg-muted rounded text-xs font-mono">Settings → Integrations</code></span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">2.</span>
                    <span>Click <strong className="text-foreground">Create New Integration</strong> and select <strong className="text-foreground">Internal Integration</strong></span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">3.</span>
                    <span>Name it (e.g. <strong className="text-foreground">Aurora</strong>)</span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">4.</span>
                    <span>Set <strong className="text-foreground">Webhook URL</strong> to:</span>
                  </li>
                </ol>

                {webhookUrl && (
                  <div className="ml-6 flex items-center gap-2">
                    <Input
                      readOnly
                      value={webhookUrl}
                      className="font-mono text-xs h-8"
                    />
                    <Button
                      variant="outline"
                      size="icon"
                      className="h-8 w-8 shrink-0"
                      onClick={handleCopyWebhook}
                      title="Copy webhook URL"
                    >
                      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                    </Button>
                  </div>
                )}

                <ol className="list-none space-y-2 text-sm text-muted-foreground" start={5}>
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">5.</span>
                    <span>Enable <strong className="text-foreground">Alert Rule Action</strong></span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">6.</span>
                    <span>
                      <span className="block">Under Permissions, set:</span>
                      <span className="block ml-4 mt-1.5 space-y-1">
                        <span className="flex items-center gap-2"><span className="text-muted-foreground">•</span><code className="px-1.5 py-0.5 bg-muted rounded text-xs font-mono">Issue & Event: Read</code></span>
                        <span className="flex items-center gap-2"><span className="text-muted-foreground">•</span><code className="px-1.5 py-0.5 bg-muted rounded text-xs font-mono">Project: Read</code></span>
                        <span className="flex items-center gap-2"><span className="text-muted-foreground">•</span><code className="px-1.5 py-0.5 bg-muted rounded text-xs font-mono">Organization: Read</code></span>
                        <span className="flex items-center gap-2"><span className="text-muted-foreground">•</span><code className="px-1.5 py-0.5 bg-muted rounded text-xs font-mono">Alerts: Read</code></span>
                      </span>
                    </span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">7.</span>
                    <span>
                      <span className="block">Under Webhooks, enable:</span>
                      <span className="block ml-4 mt-1.5 space-y-1">
                        <span className="flex items-center gap-2"><span className="text-muted-foreground">•</span><code className="px-1.5 py-0.5 bg-muted rounded text-xs font-mono">issue</code></span>
                        <span className="flex items-center gap-2"><span className="text-muted-foreground">•</span><code className="px-1.5 py-0.5 bg-muted rounded text-xs font-mono">error</code></span>
                      </span>
                    </span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <span className="font-mono text-xs mt-0.5 w-4 text-right shrink-0 text-foreground font-bold">8.</span>
                    <span>Save, then scroll down to the <strong className="text-foreground">Tokens</strong> section, click <strong className="text-foreground">New Token</strong>, and <span className="text-foreground font-semibold">copy it — it won&apos;t appear again</span></span>
                  </li>
                </ol>
              </div>

              <form onSubmit={handleConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="authToken">Internal Integration Token</Label>
                  <Input
                    id="authToken"
                    type="password"
                    placeholder="Paste your token here"
                    value={authToken}
                    onChange={(e) => setAuthToken(e.target.value)}
                    required
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="clientSecret">Client Secret</Label>
                  <Input
                    id="clientSecret"
                    type="password"
                    placeholder="Paste the Client Secret from your integration"
                    value={clientSecret}
                    onChange={(e) => setClientSecret(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Found at the top of your Internal Integration settings page
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="orgSlug">Organization Slug</Label>
                  <Input
                    id="orgSlug"
                    type="text"
                    placeholder="my-org"
                    value={orgSlug}
                    onChange={(e) => setOrgSlug(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Found in your URL: <code className="px-1 py-0.5 bg-muted rounded font-mono">sentry.io/organizations/<strong>your-slug</strong>/</code>
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="baseUrl">Base URL <span className="text-muted-foreground font-normal">(self-hosted only)</span></Label>
                  <Input
                    id="baseUrl"
                    type="text"
                    placeholder="https://sentry.your-company.com"
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    Leave empty for Sentry SaaS — only fill this for self-hosted instances
                  </p>
                </div>

                <Button type="submit" disabled={loading || !authToken || !clientSecret || !orgSlug} className="w-full">
                  {loading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Connecting...
                    </>
                  ) : (
                    'Connect Sentry'
                  )}
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : status ? (
          <>
            <Card className="mb-6">
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="h-2.5 w-2.5 rounded-full bg-green-500" />
                    <CardTitle className="text-lg">
                      {status.orgName || status.orgSlug}
                    </CardTitle>
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => loadEvents(eventsOffset)}>
                      Refresh
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={handleDisconnect}
                      disabled={loading}
                    >
                      {loading ? 'Disconnecting...' : 'Disconnect'}
                    </Button>
                  </div>
                </div>
                <CardDescription>
                  Sentry is connected and receiving alerts via webhook.
                </CardDescription>
              </CardHeader>
            </Card>

            <div className="mb-4">
              <h2 className="text-xl font-semibold">Recent Alerts</h2>
              <p className="text-sm text-muted-foreground">Events received from Sentry webhooks</p>
            </div>

            {eventsLoading ? (
              <Card>
                <CardContent className="pt-6 text-center py-12">
                  <Loader2 className="h-6 w-6 animate-spin mx-auto mb-2 text-muted-foreground" />
                  <p className="text-muted-foreground">Loading alerts...</p>
                </CardContent>
              </Card>
            ) : events.length === 0 ? (
              <Card>
                <CardContent className="pt-6 text-center py-12">
                  <p className="text-muted-foreground font-medium">No alerts received yet</p>
                  <p className="text-sm text-muted-foreground mt-2">
                    Alerts will appear here once Sentry sends webhook events
                  </p>
                </CardContent>
              </Card>
            ) : (
              <>
                <div className="space-y-3">
                  {events.map((event) => (
                    <Card key={event.id}>
                      <CardContent className="py-4">
                        <div className="flex items-start justify-between">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 mb-1">
                              <span className="font-medium text-sm truncate">
                                {event.title || 'Untitled Event'}
                              </span>
                              {event.status && (
                                <span className={`px-2 py-0.5 rounded text-xs font-medium ${getEventBadgeColor(event.status)}`}>
                                  {event.status}
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-3 text-xs text-muted-foreground">
                              {event.eventType && <span>{event.eventType}</span>}
                              {event.scope && <span>{event.scope}</span>}
                              {event.receivedAt && <span>{formatDate(event.receivedAt)}</span>}
                            </div>
                          </div>
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                </div>

                {eventsTotal > eventsLimit && (
                  <div className="flex items-center justify-between mt-6">
                    <p className="text-sm text-muted-foreground">
                      Showing {eventsOffset + 1} to {Math.min(eventsOffset + eventsLimit, eventsTotal)} of {eventsTotal}
                    </p>
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => loadEvents(Math.max(0, eventsOffset - eventsLimit))}
                        disabled={eventsOffset === 0}
                      >
                        Previous
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => loadEvents(eventsOffset + eventsLimit)}
                        disabled={eventsOffset + eventsLimit >= eventsTotal}
                      >
                        Next
                      </Button>
                    </div>
                  </div>
                )}
              </>
            )}
          </>
        ) : null}
      </div>
    </ConnectorAuthGuard>
  );
}

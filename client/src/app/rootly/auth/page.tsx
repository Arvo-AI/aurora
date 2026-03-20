"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { rootlyService, RootlyStatus, RootlyWebhookUrlResponse } from "@/lib/services/rootly";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Loader2, ExternalLink, Copy, CheckCircle2, Check } from "lucide-react";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

function persistStatus(connected: boolean) {
  if (connected) {
    localStorage.setItem("isRootlyConnected", "true");
  } else {
    localStorage.removeItem("isRootlyConnected");
  }
  window.dispatchEvent(new CustomEvent("providerStateChanged"));
}

export default function RootlyAuthPage() {
  const { toast } = useToast();
  const [apiToken, setApiToken] = useState("");
  const [status, setStatus] = useState<RootlyStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const [webhookInfo, setWebhookInfo] = useState<RootlyWebhookUrlResponse | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const cached = localStorage.getItem("isRootlyConnected") === "true";
    if (cached) {
      setStatus({ connected: true });
      setIsCheckingStatus(false);
    }

    rootlyService.getStatus()
      .then((result) => {
        if (!result) return;
        setStatus(result);
        persistStatus(result.connected);
        if (result.connected) loadWebhookUrl();
      })
      .catch((err) => console.error("Failed to load Rootly status", err))
      .finally(() => setIsCheckingStatus(false));
  }, []);

  const loadWebhookUrl = async () => {
    try {
      const info = await rootlyService.getWebhookUrl();
      if (info) setWebhookInfo(info);
    } catch (err) {
      console.error("Failed to load webhook URL", err);
    }
  };

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      const result = await rootlyService.connect(apiToken);
      setStatus(result);
      persistStatus(result.connected);
      toast({ title: "Connected", description: "Rootly connected successfully." });
      await loadWebhookUrl();

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', provider: 'rootly' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: ['rootly'] } }));
      } catch (prefErr: unknown) {
        console.warn('[rootly] Failed to update provider preferences', prefErr);
      }
    } catch (err: unknown) {
      toast({ title: "Connection failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setApiToken("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      await rootlyService.disconnect();
      setStatus({ connected: false });
      setWebhookInfo(null);
      persistStatus(false);
      toast({ title: "Disconnected", description: "Rootly disconnected successfully." });

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', provider: 'rootly' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: [] } }));
      } catch (prefErr: unknown) {
        console.warn('[rootly] Failed to update provider preferences', prefErr);
      }
    } catch (err: unknown) {
      toast({ title: "Disconnect failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhookUrl = async () => {
    if (!webhookInfo?.webhookUrl) return;
    try {
      await copyToClipboard(webhookInfo.webhookUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ title: "Copy failed", description: "Unable to copy to clipboard", variant: "destructive" });
    }
  };

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="Rootly">
        <div className="container mx-auto py-8 px-4 max-w-2xl">
          <div className="mb-6">
            <h1 className="text-3xl font-bold">Rootly Integration</h1>
            <p className="text-muted-foreground mt-1">Connect your Rootly account for incident management</p>
          </div>
          <Card>
            <CardContent className="flex items-center justify-center py-12">
              <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            </CardContent>
          </Card>
        </div>
      </ConnectorAuthGuard>
    );
  }

  return (
    <ConnectorAuthGuard connectorName="Rootly">
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Rootly Integration</h1>
          <p className="text-muted-foreground mt-1">Connect your Rootly account for incident management</p>
        </div>

        {!status?.connected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect to Rootly</CardTitle>
              <CardDescription>Enter your Rootly API key to connect.</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="apiToken">API Key</Label>
                  <Input
                    id="apiToken"
                    type="password"
                    placeholder="Enter your Rootly API key"
                    value={apiToken}
                    onChange={(e) => setApiToken(e.target.value)}
                    required
                  />
                </div>

                <div className="bg-muted/50 rounded-lg p-4 text-sm">
                  <p className="font-medium mb-2">How to get your API key:</p>
                  <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                    <li>Log in to your Rootly account</li>
                    <li>Go to <strong>Organization Settings &gt; API Keys</strong></li>
                    <li>Click <strong>Generate New API Key</strong></li>
                    <li>Copy the key and paste it above</li>
                  </ol>
                  <p className="text-xs text-muted-foreground mt-2">
                    Rootly supports Global, Team, and Personal API keys.
                  </p>
                  <a
                    href="https://docs.rootly.com/integrations/api"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2"
                  >
                    View Rootly documentation <ExternalLink className="h-3 w-3" />
                  </a>
                </div>

                <Button type="submit" className="w-full" disabled={loading || !apiToken}>
                  {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Connecting...</> : "Connect to Rootly"}
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <CardTitle className="flex items-center gap-2">
                    <CheckCircle2 className="h-5 w-5 text-green-500" />
                    Rootly Connected
                  </CardTitle>
                  <Badge variant="outline" className="bg-green-50 dark:bg-green-950/20 text-green-700 dark:text-green-400 border-green-200 dark:border-green-800">
                    <Check className="h-3 w-3 mr-1" />
                    Connected
                  </Badge>
                </div>
                <CardDescription>
                  {status.userEmail || status.userName
                    ? `Connected as ${status.userEmail || status.userName}`
                    : "Your Rootly account is connected"}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {(status.userEmail || status.userName) && (
                  <div className="grid md:grid-cols-2 gap-4 mb-6">
                    {status.userName && (
                      <div className="p-4 border rounded-lg">
                        <p className="text-xs text-muted-foreground uppercase tracking-wide">User</p>
                        <p className="text-base font-semibold">{status.userName}</p>
                      </div>
                    )}
                    {status.userEmail && (
                      <div className="p-4 border rounded-lg">
                        <p className="text-xs text-muted-foreground uppercase tracking-wide">Email</p>
                        <p className="text-base font-semibold break-all">{status.userEmail}</p>
                      </div>
                    )}
                  </div>
                )}
                <Button variant="destructive" onClick={handleDisconnect} disabled={loading}>
                  {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Disconnecting...</> : "Disconnect"}
                </Button>
              </CardContent>
            </Card>

            {webhookInfo && (
              <Card>
                <CardHeader>
                  <CardTitle>Webhook Configuration</CardTitle>
                  <CardDescription>Configure Rootly to send incident notifications to Aurora.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <Label>Webhook URL</Label>
                      <Badge variant="outline">Per user</Badge>
                    </div>
                    <div className="flex gap-2">
                      <code className="flex-1 px-3 py-2 rounded bg-muted text-xs break-all border">{webhookInfo.webhookUrl}</code>
                      <Button variant={copied ? "secondary" : "outline"} size="sm" onClick={handleCopyWebhookUrl}>
                        {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                      </Button>
                    </div>
                  </div>

                  <div className="bg-muted/50 rounded-lg p-4 text-sm">
                    <p className="font-medium mb-2">Setup Instructions:</p>
                    <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                      {webhookInfo.instructions.map((step, i) => (
                        <li key={i}>{step.replace(/^\d+\.\s*/, '')}</li>
                      ))}
                    </ol>
                    <a
                      href="https://docs.rootly.com/configuration/webhooks"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:underline mt-3 text-xs"
                    >
                      Rootly Webhook Docs <ExternalLink className="w-3 h-3" />
                    </a>
                  </div>
                </CardContent>
              </Card>
            )}
          </div>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

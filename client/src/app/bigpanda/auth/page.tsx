"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { bigpandaService, BigPandaStatus, BigPandaWebhookUrlResponse } from "@/lib/services/bigpanda";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2, ExternalLink, Copy, CheckCircle2 } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";

const CACHE_KEY = "bigpanda_connection_status";

function persistStatus(result: BigPandaStatus | null) {
  if (result?.connected) {
    localStorage.setItem(CACHE_KEY, JSON.stringify(result));
    localStorage.setItem("isBigPandaConnected", "true");
  } else {
    localStorage.removeItem(CACHE_KEY);
    localStorage.removeItem("isBigPandaConnected");
  }
  window.dispatchEvent(new CustomEvent("providerStateChanged"));
}

export default function BigPandaAuthPage() {
  const { toast } = useToast();
  const [apiToken, setApiToken] = useState("");
  const [status, setStatus] = useState<BigPandaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const [webhookInfo, setWebhookInfo] = useState<BigPandaWebhookUrlResponse | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const cached = localStorage.getItem(CACHE_KEY);
    if (cached) {
      try {
        const parsed: BigPandaStatus = JSON.parse(cached);
        setStatus(parsed);
        setIsCheckingStatus(false);
      } catch {
        localStorage.removeItem(CACHE_KEY);
      }
    }

    bigpandaService.getStatus()
      .then((result) => {
        if (!result) return;
        setStatus(result);
        persistStatus(result);
        if (result.connected) loadWebhookUrl();
      })
      .catch((err) => console.error("Failed to load BigPanda status", err))
      .finally(() => setIsCheckingStatus(false));
  }, []);

  const loadWebhookUrl = async () => {
    const info = await bigpandaService.getWebhookUrl();
    if (info) setWebhookInfo(info);
  };

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      const result = await bigpandaService.connect(apiToken);
      setStatus(result);
      persistStatus(result);
      toast({ title: "Connected", description: "BigPanda connected successfully." });
      loadWebhookUrl();
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
      await bigpandaService.disconnect();
      setStatus({ connected: false });
      setWebhookInfo(null);
      persistStatus(null);
      toast({ title: "Disconnected", description: "BigPanda disconnected successfully." });
    } catch (err: unknown) {
      toast({ title: "Disconnect failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhookUrl = () => {
    if (!webhookInfo?.webhookUrl) return;
    navigator.clipboard.writeText(webhookInfo.webhookUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const pageHeader = (
    <div className="mb-6">
      <h1 className="text-3xl font-bold">BigPanda Integration</h1>
      <p className="text-muted-foreground mt-1">Connect your BigPanda account for incident correlation</p>
    </div>
  );

  if (isCheckingStatus) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        {pageHeader}
        <Card>
          <CardContent className="flex items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-2xl">
      {pageHeader}

      {!status?.connected ? (
        <Card>
          <CardHeader>
            <CardTitle>Connect to BigPanda</CardTitle>
            <CardDescription>Enter your BigPanda API token to connect.</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleConnect} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="apiToken">API Token</Label>
                <Input
                  id="apiToken"
                  type="password"
                  placeholder="Enter your BigPanda API token"
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                  required
                />
              </div>

              <div className="bg-muted/50 rounded-lg p-4 text-sm">
                <p className="font-medium mb-2">How to get your API token:</p>
                <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                  <li>Log in to your BigPanda account</li>
                  <li>Go to Settings &gt; API Keys</li>
                  <li>Copy your User API Key or create a new one</li>
                </ol>
                <a
                  href="https://docs.bigpanda.io/reference/authentication"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2"
                >
                  View BigPanda documentation <ExternalLink className="h-3 w-3" />
                </a>
              </div>

              <Button type="submit" className="w-full" disabled={loading || !apiToken}>
                {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Connecting...</> : "Connect to BigPanda"}
              </Button>
            </form>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <CheckCircle2 className="h-5 w-5 text-green-500" />
                BigPanda Connected
              </CardTitle>
              <CardDescription>
                {status.environmentCount != null
                  ? `${status.environmentCount} environment${status.environmentCount !== 1 ? 's' : ''} detected`
                  : "Your BigPanda account is connected"}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button variant="destructive" onClick={handleDisconnect} disabled={loading}>
                {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Disconnecting...</> : "Disconnect"}
              </Button>
            </CardContent>
          </Card>

          {webhookInfo && (
            <Card>
              <CardHeader>
                <CardTitle>Webhook Configuration</CardTitle>
                <CardDescription>Configure BigPanda to send incident notifications to Aurora.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <Label>Webhook URL</Label>
                  <div className="flex gap-2">
                    <Input
                      readOnly
                      value={webhookInfo.webhookUrl}
                      className="font-mono text-sm"
                    />
                    <Button variant="outline" size="icon" onClick={handleCopyWebhookUrl}>
                      {copied ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
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
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { prometheusService, PrometheusStatus, PrometheusWebhookUrlResponse } from "@/lib/services/prometheus";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2, ExternalLink, Copy, CheckCircle2 } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";

function persistStatus(connected: boolean) {
  if (connected) {
    localStorage.setItem("isPrometheusConnected", "true");
  } else {
    localStorage.removeItem("isPrometheusConnected");
  }
  window.dispatchEvent(new CustomEvent("providerStateChanged"));
}

export default function PrometheusAuthPage() {
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [status, setStatus] = useState<PrometheusStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const [webhookInfo, setWebhookInfo] = useState<PrometheusWebhookUrlResponse | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const cached = localStorage.getItem("isPrometheusConnected") === "true";
    if (cached) {
      setStatus({ connected: true });
      setIsCheckingStatus(false);
    }

    prometheusService.getStatus()
      .then((result) => {
        if (!result) return;
        setStatus(result);
        persistStatus(result.connected);
        if (result.connected) loadWebhookUrl();
      })
      .catch((err) => console.error("Failed to load Prometheus status", err))
      .finally(() => setIsCheckingStatus(false));
  }, []);

  const loadWebhookUrl = async () => {
    try {
      const info = await prometheusService.getWebhookUrl();
      if (info) setWebhookInfo(info);
    } catch (err) {
      console.error("Failed to load webhook URL", err);
    }
  };

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      const result = await prometheusService.connect(baseUrl, apiToken || undefined);
      setStatus(result);
      persistStatus(result.connected);
      toast({ title: "Connected", description: `Prometheus ${result.version ? `v${result.version} ` : ""}connected successfully.` });
      await loadWebhookUrl();
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
      await prometheusService.disconnect();
      setStatus({ connected: false });
      setWebhookInfo(null);
      persistStatus(false);
      toast({ title: "Disconnected", description: "Prometheus disconnected successfully." });
    } catch (err: unknown) {
      toast({ title: "Disconnect failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhookUrl = async () => {
    if (!webhookInfo?.webhookUrl) return;
    try {
      await navigator.clipboard.writeText(webhookInfo.webhookUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ title: "Copy failed", description: "Unable to copy to clipboard", variant: "destructive" });
    }
  };

  const pageHeader = (
    <div className="mb-6">
      <h1 className="text-3xl font-bold">Prometheus Integration</h1>
      <p className="text-muted-foreground mt-1">Connect your Prometheus instance to receive alerts and query metrics</p>
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
            <CardTitle>Connect to Prometheus</CardTitle>
            <CardDescription>Enter your Prometheus server URL to connect. API token is optional for unauthenticated instances.</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleConnect} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="baseUrl">Prometheus URL</Label>
                <Input
                  id="baseUrl"
                  type="url"
                  placeholder="https://prometheus.example.com"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  required
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="apiToken">API Token (optional)</Label>
                <Input
                  id="apiToken"
                  type="password"
                  placeholder="Leave empty if not using authentication"
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                />
              </div>

              <div className="bg-muted/50 rounded-lg p-4 text-sm">
                <p className="font-medium mb-2">How to connect:</p>
                <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                  <li>Enter your Prometheus server URL (e.g., https://prometheus.example.com)</li>
                  <li>If your instance requires authentication, provide a Bearer token</li>
                  <li>After connecting, configure Alertmanager to send webhooks to Aurora</li>
                </ol>
                <a
                  href="https://prometheus.io/docs/alerting/latest/configuration/#webhook_config"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2"
                >
                  Alertmanager webhook docs <ExternalLink className="h-3 w-3" />
                </a>
              </div>

              <Button type="submit" className="w-full" disabled={loading || !baseUrl}>
                {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Connecting...</> : "Connect to Prometheus"}
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
                Prometheus Connected
              </CardTitle>
              <CardDescription>
                {status.version
                  ? `Connected to Prometheus v${status.version}`
                  : "Your Prometheus instance is connected"}
                {status.baseUrl && <span className="block mt-1 font-mono text-xs">{status.baseUrl}</span>}
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
                <CardTitle>Alertmanager Webhook</CardTitle>
                <CardDescription>Configure your Alertmanager to send alerts to Aurora.</CardDescription>
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
                  <p className="font-medium mb-2">Alertmanager Configuration:</p>
                  <pre className="bg-background rounded p-3 text-xs overflow-x-auto mb-3 border">
{`receivers:
  - name: 'aurora'
    webhook_configs:
      - url: '${webhookInfo.webhookUrl}'

route:
  receiver: 'aurora'
  # Or add as a sub-route for specific alerts`}
                  </pre>
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

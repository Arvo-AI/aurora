"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { dynatraceService, DynatraceStatus } from "@/lib/services/dynatrace";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2, ExternalLink } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { DynatraceWebhookStep } from "@/components/dynatrace/DynatraceWebhookStep";

const CACHE_KEY = "dynatrace_connection_status";

export default function DynatraceAuthPage() {
  const { toast } = useToast();
  const [environmentUrl, setEnvironmentUrl] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [status, setStatus] = useState<DynatraceStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        if (typeof window !== "undefined") {
          const cached = localStorage.getItem(CACHE_KEY);
          if (cached) {
            const parsed = JSON.parse(cached);
            setStatus(parsed);
            setIsCheckingStatus(false);
            if (parsed?.connected) setEnvironmentUrl(parsed.environmentUrl ?? "");
          }
        }
        const result = await dynatraceService.getStatus();
        if (result) {
          setStatus(result);
          if (typeof window !== "undefined") {
            localStorage.setItem(CACHE_KEY, JSON.stringify(result));
            if (result.connected) {
              localStorage.setItem("isDynatraceConnected", "true");
            } else {
              localStorage.removeItem("isDynatraceConnected");
            }
            window.dispatchEvent(new CustomEvent("providerStateChanged"));
          }
          if (result.connected) setEnvironmentUrl(result.environmentUrl ?? "");
        }
      } catch (err) {
        console.error("Failed to load Dynatrace status", err);
      } finally {
        setIsCheckingStatus(false);
      }
    })();
  }, []);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      const result = await dynatraceService.connect({ environmentUrl, apiToken });
      setStatus(result);
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        localStorage.setItem("isDynatraceConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
      toast({ title: "Success", description: "Dynatrace connected successfully!" });
    } catch (err: unknown) {
      toast({ title: "Failed to connect to Dynatrace", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setApiToken("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/dynatrace", { method: "DELETE", credentials: "include" });
      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setEnvironmentUrl("");
        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEY);
          localStorage.removeItem("isDynatraceConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }
        toast({ title: "Success", description: "Dynatrace disconnected successfully" });
      } else {
        throw new Error(await response.text() || "Failed to disconnect Dynatrace");
      }
    } catch (err: unknown) {
      toast({ title: "Failed to disconnect Dynatrace", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  if (isCheckingStatus) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Dynatrace Integration</h1>
          <p className="text-muted-foreground mt-1">Connect your Dynatrace environment</p>
        </div>
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
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Dynatrace Integration</h1>
        <p className="text-muted-foreground mt-1">Connect your Dynatrace environment</p>
      </div>

      {!status?.connected ? (
        <Card>
          <CardHeader>
            <CardTitle>Connect to Dynatrace</CardTitle>
            <CardDescription>Enter your Dynatrace environment URL and API token.</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleConnect} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="environmentUrl">Environment URL</Label>
                <Input
                  id="environmentUrl"
                  type="url"
                  placeholder="https://abc12345.live.dynatrace.com"
                  value={environmentUrl}
                  onChange={(e) => setEnvironmentUrl(e.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Your Dynatrace SaaS or Managed environment URL
                </p>
              </div>

              <div className="bg-muted/50 rounded-lg p-3 text-xs space-y-1">
                <p className="font-medium">Example URLs:</p>
                <ul className="text-muted-foreground space-y-0.5">
                  <li><strong>SaaS:</strong> https://abc12345.live.dynatrace.com</li>
                  <li><strong>Managed:</strong> https://your-domain.com/e/environment-id</li>
                </ul>
              </div>

              <div className="space-y-2">
                <Label htmlFor="apiToken">API Token</Label>
                <Input
                  id="apiToken"
                  type="password"
                  placeholder="Enter your Dynatrace API token"
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                  required
                />
              </div>

              <div className="bg-muted/50 rounded-lg p-4 text-sm">
                <p className="font-medium mb-2">How to create an API token:</p>
                <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                  <li>Go to Settings &gt; Access tokens in Dynatrace</li>
                  <li>Click &quot;Generate new token&quot;</li>
                  <li>Add scopes: <code className="bg-muted px-1 rounded">problems.read</code>, <code className="bg-muted px-1 rounded">events.ingest</code>, <code className="bg-muted px-1 rounded">logs.ingest</code>, <code className="bg-muted px-1 rounded">metrics.read</code>, <code className="bg-muted px-1 rounded">entities.read</code></li>
                  <li>Copy the generated token</li>
                </ol>
                <a
                  href="https://docs.dynatrace.com/docs/dynatrace-api/basics/dynatrace-api-authentication"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2"
                >
                  View Dynatrace documentation <ExternalLink className="h-3 w-3" />
                </a>
              </div>

              <Button type="submit" className="w-full" disabled={loading || !environmentUrl || !apiToken}>
                {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Connecting...</> : "Connect to Dynatrace"}
              </Button>
            </form>
          </CardContent>
        </Card>
      ) : (
        <DynatraceWebhookStep status={status} onDisconnect={handleDisconnect} loading={loading} />
      )}
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { lokiService, LokiStatus } from "@/lib/services/loki";
import { getUserFriendlyError } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ExternalLink, CheckCircle2, Database } from "lucide-react";

const CACHE_KEYS = {
  STATUS: "loki_connection_status",
};

export default function LokiAuthPage() {
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [username, setUsername] = useState("");
  const [status, setStatus] = useState<LokiStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== "undefined") {
        const cachedStatus = localStorage.getItem(CACHE_KEYS.STATUS);

        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus);
          setStatus(parsedStatus);
          if (parsedStatus?.connected) {
            setBaseUrl(parsedStatus.baseUrl ?? "");
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
    } catch (err) {
      console.error("Failed to load Loki status", err);
      toast({
        title: "Error",
        description: "Unable to load Loki status",
        variant: "destructive",
      });
    }
  };

  const fetchAndUpdateStatus = async () => {
    const result = await lokiService.getStatus();
    setStatus(result);

    if (typeof window !== "undefined") {
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
    }

    if (result?.connected) {
      setBaseUrl(result.baseUrl ?? "");
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = {
        baseUrl,
        apiToken,
        username: username || undefined,
      };
      const result = await lokiService.connect(payload);
      setStatus(result);

      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
      }

      toast({
        title: "Success",
        description: "Loki connected successfully!",
      });

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "loki" }),
        });
      } catch (prefErr) {
        console.warn("Failed to update provider preferences", prefErr);
      }

      if (typeof window !== "undefined") {
        localStorage.setItem("isLokiConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
        window.dispatchEvent(
          new CustomEvent("providerPreferenceChanged", {
            detail: { providers: ["loki"] },
          })
        );
      }
    } catch (err: any) {
      console.error("Loki connection failed", err);
      const errorMessage = getUserFriendlyError(err);
      toast({
        title: "Failed to connect to Loki",
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
      setApiToken("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);

    try {
      const response = await fetch("/api/connected-accounts/loki", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setBaseUrl("");
        setUsername("");

        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEYS.STATUS);
        }

        toast({
          title: "Success",
          description: "Loki disconnected successfully",
        });

        if (typeof window !== "undefined") {
          localStorage.removeItem("isLokiConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
          window.dispatchEvent(
            new CustomEvent("providerPreferenceChanged", {
              detail: { providers: [] },
            })
          );
        }

        try {
          await fetch("/api/provider-preferences", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "remove", provider: "loki" }),
          });
        } catch (prefErr) {
          console.warn("Failed to update provider preferences", prefErr);
        }
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Loki");
      }
    } catch (err: any) {
      console.error("Loki disconnect failed", err);
      const errorMessage = getUserFriendlyError(err);
      toast({
        title: "Failed to disconnect Loki",
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <ConnectorAuthGuard connectorName="Loki">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-amber-100 dark:bg-amber-900/30">
            <Database className="h-5 w-5 text-amber-700 dark:text-amber-400" />
          </div>
          <div>
            <h1 className="text-3xl font-bold">Loki Integration</h1>
            <p className="text-muted-foreground mt-1">
              Connect your Grafana Loki instance to query logs
            </p>
          </div>
        </div>

        {!status?.connected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect to Loki</CardTitle>
              <CardDescription>
                Enter your Loki endpoint URL and credentials to connect.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="loki-base-url">Loki URL</Label>
                  <Input
                    id="loki-base-url"
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="https://logs-prod-us-central1.grafana.net"
                    required
                    disabled={loading}
                  />
                  <p className="text-xs text-muted-foreground">
                    Grafana Cloud: your Loki endpoint URL. Self-hosted: e.g. https://loki.internal:3100
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="loki-username">
                    Username / Tenant ID{" "}
                    <span className="text-muted-foreground font-normal">(optional)</span>
                  </Label>
                  <Input
                    id="loki-username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="123456"
                    disabled={loading}
                  />
                  <p className="text-xs text-muted-foreground">
                    Grafana Cloud: your numeric instance ID. Self-hosted multi-tenant: your org ID. Leave blank for single-tenant.
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="loki-token">API Token</Label>
                  <Input
                    id="loki-token"
                    type="password"
                    value={apiToken}
                    onChange={(e) => setApiToken(e.target.value)}
                    placeholder="glc_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                    required
                    disabled={loading}
                  />
                </div>

                <div className="bg-muted/50 rounded-lg p-4 text-sm">
                  <p className="font-medium mb-2">How to get your credentials:</p>
                  <div className="space-y-3">
                    <div>
                      <p className="font-medium text-xs uppercase tracking-wide text-muted-foreground mb-1">Grafana Cloud</p>
                      <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                        <li>Go to <strong className="text-foreground">grafana.com</strong> &gt; your stack &gt; Loki details</li>
                        <li>Copy the <strong className="text-foreground">URL</strong> and <strong className="text-foreground">Instance ID</strong> (username)</li>
                        <li>Create an <strong className="text-foreground">Access Policy</strong> with <code className="text-xs bg-muted px-1 rounded">logs:read</code> scope</li>
                        <li>Generate a token from that policy and paste it above</li>
                      </ol>
                    </div>
                    <div>
                      <p className="font-medium text-xs uppercase tracking-wide text-muted-foreground mb-1">Self-hosted</p>
                      <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                        <li>Enter your Loki URL (e.g. <code className="text-xs bg-muted px-1 rounded">https://loki.internal:3100</code>)</li>
                        <li>If behind a reverse proxy with basic auth, enter those credentials</li>
                        <li>For multi-tenant setups, enter the tenant/org ID as the username</li>
                      </ol>
                    </div>
                  </div>
                  <a
                    href="https://grafana.com/docs/grafana-cloud/reference/create-api-key/"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-3 text-xs"
                  >
                    Grafana Cloud access policy docs <ExternalLink className="h-3 w-3" />
                  </a>
                </div>

                <Button
                  type="submit"
                  className="w-full"
                  disabled={loading || !apiToken || !baseUrl}
                >
                  {loading ? "Connecting..." : "Connect Loki"}
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader>
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-5 w-5 text-green-500" />
                <CardTitle>Loki Connected</CardTitle>
              </div>
              <CardDescription>
                Your Loki instance is connected and ready to query logs.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="bg-muted/50 rounded-lg p-4 space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Endpoint</span>
                  <span className="font-mono text-xs">{status.baseUrl}</span>
                </div>
                {status.username && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Username / Tenant</span>
                    <span className="font-mono text-xs">{status.username}</span>
                  </div>
                )}
                {status.labelCount !== undefined && status.labelCount !== null && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Labels discovered</span>
                    <span>{status.labelCount}</span>
                  </div>
                )}
              </div>

              <Button
                variant="destructive"
                onClick={handleDisconnect}
                disabled={loading}
                className="w-full"
              >
                {loading ? "Disconnecting..." : "Disconnect Loki"}
              </Button>
            </CardContent>
          </Card>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

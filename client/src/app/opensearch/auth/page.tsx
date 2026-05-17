"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { openSearchService, OpenSearchStatus } from "@/lib/services/opensearch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from "@/components/ui/card";
import { Loader2, CheckCircle, ExternalLink, Database } from "lucide-react";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEY = "opensearch_connection_status";

export default function OpenSearchAuthPage() {
  const { toast } = useToast();
  const [endpoint, setEndpoint] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [indexPattern, setIndexPattern] = useState("*");
  const [status, setStatus] = useState<OpenSearchStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);

  const loadStatus = async () => {
    try {
      if (typeof window !== "undefined") {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          const parsed = JSON.parse(cached);
          setStatus(parsed);
          if (parsed?.connected) setEndpoint(parsed.endpoint ?? "");
        }
      }
      const result = await openSearchService.getStatus();
      if (result !== null) {
        setStatus(result);
        if (typeof window !== "undefined") {
          localStorage.setItem(CACHE_KEY, JSON.stringify({ connected: result.connected }));
          if (result.connected) setEndpoint(result.endpoint ?? "");
        }
      }
    } catch (err) {
      console.error("[OpenSearch] Failed to load status", err);
    } finally {
      setIsCheckingStatus(false);
    }
  };

  useEffect(() => { loadStatus(); }, []);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      const result = await openSearchService.connect({
        endpoint,
        username,
        password,
        indexPattern: indexPattern || "*",
      });
      setStatus(result);
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify({ connected: result.connected }));
        localStorage.setItem("isOpenSearchConnected", "true");
        window.dispatchEvent(new Event("openSearchStateChanged"));
        window.dispatchEvent(new Event("providerStateChanged"));
      }
      toast({ title: "OpenSearch connected", description: `Cluster: ${result.clusterName ?? endpoint}` });
      setPassword("");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Connection failed";
      toast({ title: "Failed to connect", description: msg, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/opensearch/disconnect", {
        method: "DELETE",
        credentials: "include",
      });
      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setEndpoint("");
        setUsername("");
        setPassword("");
        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEY);
          localStorage.removeItem("isOpenSearchConnected");
          window.dispatchEvent(new Event("openSearchStateChanged"));
          window.dispatchEvent(new Event("providerStateChanged"));
        }
        toast({ title: "OpenSearch disconnected" });
      } else {
        throw new Error("Failed to disconnect");
      }
    } catch (err: unknown) {
      toast({ title: "Failed to disconnect", description: err instanceof Error ? err.message : "Unknown error", variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="OpenSearch">
        <div className="container mx-auto py-8 px-4 max-w-2xl">
          <div className="mb-6">
            <h1 className="text-3xl font-bold">OpenSearch Integration</h1>
            <p className="text-muted-foreground mt-1">Connect your OpenSearch cluster for log search during RCA</p>
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
    <ConnectorAuthGuard connectorName="OpenSearch">
      <div className="container mx-auto py-8 px-4 max-w-2xl space-y-6">
        <div className="flex items-center gap-4">
          <div className="h-14 w-14 rounded-xl bg-white border flex items-center justify-center p-2.5 shadow-sm">
            <Database className="h-8 w-8 text-[#005EB8]" />
          </div>
          <div>
            <h1 className="text-3xl font-bold">OpenSearch</h1>
            <p className="text-muted-foreground mt-0.5">Search logs and traces during incident RCA</p>
          </div>
        </div>

        {status?.connected ? (
          <Card className="border-[#005EB8]/30 bg-[#005EB8]/[0.03]">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                  <div className="h-8 w-8 rounded-full bg-[#005EB8]/10 flex items-center justify-center">
                    <CheckCircle className="h-4 w-4 text-[#005EB8]" />
                  </div>
                  <div>
                    <CardTitle className="text-base">Connected</CardTitle>
                    <CardDescription className="text-xs">{status.endpoint}</CardDescription>
                  </div>
                </div>
                <span className="text-[10px] font-semibold uppercase tracking-wider text-[#005EB8] bg-[#005EB8]/10 px-2 py-1 rounded-full">
                  Basic Auth
                </span>
              </div>
            </CardHeader>
            <CardContent className="pt-0 pb-3">
              <div className="grid grid-cols-2 gap-3 text-sm">
                {status.clusterName && (
                  <div>
                    <p className="text-xs text-muted-foreground">Cluster</p>
                    <p className="font-medium">{status.clusterName}</p>
                  </div>
                )}
                {status.version && (
                  <div>
                    <p className="text-xs text-muted-foreground">Version</p>
                    <p className="font-medium">{status.version}</p>
                  </div>
                )}
                {status.indexPattern && (
                  <div>
                    <p className="text-xs text-muted-foreground">Index Pattern</p>
                    <p className="font-mono text-xs">{status.indexPattern}</p>
                  </div>
                )}
              </div>
            </CardContent>
            <CardFooter className="pt-0">
              <Button
                variant="ghost"
                size="sm"
                className="text-muted-foreground hover:text-destructive"
                onClick={handleDisconnect}
                disabled={loading}
              >
                {loading ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : null}
                Disconnect
              </Button>
            </CardFooter>
          </Card>
        ) : (
          <Card>
            <CardHeader>
              <CardTitle>Connect to OpenSearch</CardTitle>
              <CardDescription>
                Enter your cluster endpoint and credentials. Aurora uses Basic authentication.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleConnect} className="space-y-4">
                <div className="space-y-1.5">
                  <Label htmlFor="endpoint">Cluster Endpoint</Label>
                  <Input
                    id="endpoint"
                    type="url"
                    placeholder="https://centrallogserver.monotype.com"
                    value={endpoint}
                    onChange={(e) => setEndpoint(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">Include the full URL with port if non-standard</p>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label htmlFor="username">Username</Label>
                    <Input
                      id="username"
                      type="text"
                      placeholder="SRE"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      required
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="password">Password</Label>
                    <Input
                      id="password"
                      type="password"
                      placeholder="••••••••"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                    />
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="indexPattern">Index Pattern</Label>
                  <Input
                    id="indexPattern"
                    type="text"
                    placeholder="*"
                    value={indexPattern}
                    onChange={(e) => setIndexPattern(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    Wildcards supported, e.g. <code className="bg-muted px-1 rounded">logs-*</code> or <code className="bg-muted px-1 rounded">*</code> for all
                  </p>
                </div>

                <div className="bg-muted/50 rounded-lg p-3 text-xs text-muted-foreground space-y-1">
                  <p className="font-medium text-foreground">What Aurora uses this for:</p>
                  <ul className="list-disc list-inside space-y-0.5">
                    <li>Search logs by keyword or service name during RCA</li>
                    <li>Find error traces within the incident time window</li>
                    <li>Correlate log patterns with triggered alerts</li>
                  </ul>
                </div>

                <Button
                  type="submit"
                  className="w-full bg-[#005EB8] hover:bg-[#004d99] text-white"
                  disabled={loading || !endpoint || !username || !password}
                >
                  {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                  Connect to OpenSearch
                </Button>
              </form>
            </CardContent>
          </Card>
        )}

        <Card className="border-dashed">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">OpenSearch REST API Docs</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <a
              href="https://opensearch.org/docs/latest/api-reference/"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-xs text-[#005EB8] hover:underline"
            >
              View OpenSearch API reference <ExternalLink className="h-3 w-3" />
            </a>
          </CardContent>
        </Card>
      </div>
    </ConnectorAuthGuard>
  );
}

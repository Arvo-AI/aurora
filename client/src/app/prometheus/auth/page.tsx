"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { prometheusService, PrometheusStatus } from "@/lib/services/prometheus";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2, ExternalLink, CheckCircle2, ChevronDown, ChevronUp } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

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
  const [prometheusUrl, setPrometheusUrl] = useState("");
  const [alertmanagerUrl, setAlertmanagerUrl] = useState("");
  const [bearerToken, setBearerToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showAuth, setShowAuth] = useState(false);
  const [status, setStatus] = useState<PrometheusStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);

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
      })
      .catch((err) => console.error("Failed to load Prometheus status", err))
      .finally(() => setIsCheckingStatus(false));
  }, []);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      const result = await prometheusService.connect(
        prometheusUrl,
        alertmanagerUrl || undefined,
        bearerToken || undefined,
        username || undefined,
        password || undefined,
      );
      setStatus(result);
      persistStatus(result.connected);
      const desc = result.version
        ? `Prometheus ${result.version} connected${result.alertmanagerConnected ? " (Alertmanager connected)" : ""}.`
        : "Prometheus connected successfully.";
      toast({ title: "Connected", description: desc });
    } catch (err: unknown) {
      toast({ title: "Connection failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setBearerToken("");
      setPassword("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      await prometheusService.disconnect();
      setStatus({ connected: false });
      persistStatus(false);
      toast({ title: "Disconnected", description: "Prometheus disconnected successfully." });
    } catch (err: unknown) {
      toast({ title: "Disconnect failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const pageHeader = (
    <div className="mb-6">
      <h1 className="text-3xl font-bold">Prometheus Integration</h1>
      <p className="text-muted-foreground mt-1">Connect your Prometheus server and Alertmanager for metrics and alerting</p>
    </div>
  );

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="Prometheus">
        <div className="container mx-auto py-8 px-4 max-w-2xl">
          {pageHeader}
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
    <ConnectorAuthGuard connectorName="Prometheus">
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        {pageHeader}

        {!status?.connected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect to Prometheus</CardTitle>
              <CardDescription>Provide your Prometheus server URL and optional Alertmanager URL.</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="prometheusUrl">Prometheus Server URL</Label>
                  <Input
                    id="prometheusUrl"
                    type="url"
                    placeholder="http://prometheus:9090"
                    value={prometheusUrl}
                    onChange={(e) => setPrometheusUrl(e.target.value)}
                    required
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="alertmanagerUrl">Alertmanager URL (optional)</Label>
                  <Input
                    id="alertmanagerUrl"
                    type="url"
                    placeholder="http://alertmanager:9093"
                    value={alertmanagerUrl}
                    onChange={(e) => setAlertmanagerUrl(e.target.value)}
                  />
                </div>

                <button
                  type="button"
                  onClick={() => setShowAuth(!showAuth)}
                  className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
                >
                  {showAuth ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                  Authentication (optional)
                </button>

                {showAuth && (
                  <div className="space-y-4 pl-2 border-l-2 border-muted">
                    <div className="space-y-2">
                      <Label htmlFor="bearerToken">Bearer Token</Label>
                      <Input
                        id="bearerToken"
                        type="password"
                        placeholder="Enter bearer token"
                        value={bearerToken}
                        onChange={(e) => setBearerToken(e.target.value)}
                      />
                    </div>

                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                      <span className="h-px flex-1 bg-muted" />
                      or use basic auth
                      <span className="h-px flex-1 bg-muted" />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label htmlFor="username">Username</Label>
                        <Input
                          id="username"
                          placeholder="Username"
                          value={username}
                          onChange={(e) => setUsername(e.target.value)}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="password">Password</Label>
                        <Input
                          id="password"
                          type="password"
                          placeholder="Password"
                          value={password}
                          onChange={(e) => setPassword(e.target.value)}
                        />
                      </div>
                    </div>
                  </div>
                )}

                <div className="bg-muted/50 rounded-lg p-4 text-sm">
                  <p className="font-medium mb-2">Where to find your URLs:</p>
                  <ul className="list-disc list-inside space-y-1 text-muted-foreground">
                    <li>Prometheus typically runs on port <code className="text-xs">9090</code></li>
                    <li>Alertmanager typically runs on port <code className="text-xs">9093</code></li>
                    <li>Both must be reachable from the Aurora server</li>
                  </ul>
                  <a
                    href="https://prometheus.io/docs/prometheus/latest/querying/api/"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2"
                  >
                    View Prometheus API docs <ExternalLink className="h-3 w-3" />
                  </a>
                </div>

                <Button type="submit" className="w-full" disabled={loading || !prometheusUrl}>
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
                  {[
                    status.version ? `Version ${status.version}` : null,
                    status.alertmanagerConnected ? "Alertmanager connected" : null,
                  ].filter(Boolean).join(" · ") || "Your Prometheus server is connected"}
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Button variant="destructive" onClick={handleDisconnect} disabled={loading}>
                  {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Disconnecting...</> : "Disconnect"}
                </Button>
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

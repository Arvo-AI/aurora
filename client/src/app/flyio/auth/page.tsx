"use client";

import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Loader2, ExternalLink, AlertCircle, CheckCircle2, Shield, LogOut, Server } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { providerPreferencesService } from '@/lib/services/providerPreferences';
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

interface FlyioApp {
  name: string;
  status: string;
}

interface FlyioStatus {
  connected: boolean;
  org_slug?: string;
  tier?: "readonly" | "full";
  apps?: FlyioApp[];
}

export default function FlyioAuthPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [apiToken, setApiToken] = useState("");
  const [orgSlug, setOrgSlug] = useState("");
  const [status, setStatus] = useState<FlyioStatus | null>(null);
  const { toast } = useToast();

  const applyStatusResponse = useCallback((data: { connected?: boolean; org_slug?: string; tier?: string; apps?: FlyioApp[] }) => {
    const connected = data.connected === true;
    if (connected) {
      localStorage.setItem("isFlyioConnected", "true");
    } else {
      localStorage.removeItem("isFlyioConnected");
    }
    setStatus({
      connected,
      org_slug: data.org_slug,
      tier: data.tier as FlyioStatus["tier"],
      apps: data.apps,
    });
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadStatus() {
      // Fast cached read -- renders UI immediately
      try {
        const cached = await fetch(`/api/proxy/flyio/status`);
        if (!cancelled && cached.ok) {
          const data = await cached.json();
          applyStatusResponse(data);
        }
      } catch { /* ignore */ }
      setIsCheckingStatus(false);

      // Background live validation -- silently corrects if token expired
      try {
        const validated = await fetch(`/api/proxy/flyio/status?validate=true`);
        if (!cancelled && validated.ok) {
          const data = await validated.json();
          applyStatusResponse(data);
        } else if (!cancelled) {
          localStorage.removeItem("isFlyioConnected");
          setStatus({ connected: false });
        }
      } catch {
        if (!cancelled) {
          localStorage.removeItem("isFlyioConnected");
          setStatus({ connected: false });
        }
      }
    }

    loadStatus();
    return () => { cancelled = true; };
  }, [applyStatusResponse]);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();

    if (!apiToken || !orgSlug) {
      setError("Both API token and organization slug are required");
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/proxy/flyio/connect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apiToken, orgSlug }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to connect to Fly.io');
      }

      localStorage.setItem("isFlyioConnected", "true");
      await providerPreferencesService.smartAutoSelect('flyio', true);
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      window.dispatchEvent(new CustomEvent('providerConnectionAction'));

      toast({
        title: "Fly.io Connected",
        description: `Connected to org "${data.org_slug}" with ${data.tier === "readonly" ? "read-only" : "full"} access. Found ${data.apps?.length ?? 0} app(s).`,
      });

      setApiToken("");
      setOrgSlug("");
      setStatus({
        connected: true,
        org_slug: data.org_slug,
        tier: data.tier,
        apps: data.apps,
      });
    } catch (err: unknown) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to connect to Fly.io';
      setError(errorMessage);
    } finally {
      setIsLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      const response = await fetch(`/api/proxy/flyio/disconnect`, {
        method: 'DELETE',
      });

      if (response.ok) {
        localStorage.removeItem("isFlyioConnected");
        window.dispatchEvent(new CustomEvent('providerStateChanged'));

        toast({
          title: "Fly.io Disconnected",
          description: "Your Fly.io account has been disconnected.",
        });

        setStatus({ connected: false });
        setApiToken("");
        setOrgSlug("");
      } else {
        const data = await response.json();
        throw new Error(data.error || "Failed to disconnect");
      }
    } catch (err: unknown) {
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : "Failed to disconnect Fly.io",
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
    }
  };

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="Fly.io">
        <div className="container mx-auto py-8 px-4 max-w-3xl flex justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      </ConnectorAuthGuard>
    );
  }

  return (
    <ConnectorAuthGuard connectorName="Fly.io">
      <div className="container mx-auto py-8 px-4 max-w-3xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Fly.io Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect to Fly.io for application monitoring, machine lifecycle management, metrics, logs, and incident remediation.
          </p>
        </div>

        {status?.connected ? (
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <div className="flex items-center gap-3">
                  <CheckCircle2 className="h-5 w-5 text-green-500" />
                  <div>
                    <CardTitle>Fly.io Connected</CardTitle>
                    <CardDescription>Your Fly.io organization is linked to Aurora</CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-5">
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="flex items-center gap-3 p-3 bg-muted/50 rounded-lg">
                    <Server className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                    <div className="min-w-0">
                      <p className="text-xs text-muted-foreground">Organization</p>
                      <p className="text-sm font-medium truncate">{status.org_slug}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 p-3 bg-muted/50 rounded-lg">
                    <Shield className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                    <div className="min-w-0">
                      <p className="text-xs text-muted-foreground">Access Level</p>
                      <Badge variant={status.tier === "full" ? "default" : "secondary"} className="mt-0.5">
                        {status.tier === "full" ? "Full Access" : "Read-Only"}
                      </Badge>
                    </div>
                  </div>
                </div>

                {status.apps && status.apps.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-sm font-medium">
                      Apps ({status.apps.length})
                    </p>
                    <div className="max-h-48 overflow-y-auto rounded-lg border border-border divide-y divide-border">
                      {status.apps.map((app) => (
                        <div key={app.name} className="flex items-center justify-between px-3 py-2 text-sm">
                          <div className="flex items-center gap-2 min-w-0">
                            <Server className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                            <span className="font-mono text-xs truncate">{app.name}</span>
                          </div>
                          <Badge
                            variant={app.status === "deployed" ? "default" : "secondary"}
                            className={`text-[10px] ml-2 flex-shrink-0 ${
                              app.status === "deployed" ? "bg-green-600 hover:bg-green-700" :
                              app.status === "suspended" ? "bg-yellow-600 hover:bg-yellow-700" :
                              ""
                            }`}
                          >
                            {app.status}
                          </Badge>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {status.tier === "readonly" && (
                  <div className="p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-lg flex items-start gap-2.5">
                    <AlertCircle className="h-4 w-4 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
                    <div className="text-xs text-muted-foreground">
                      <p className="font-medium text-yellow-600 dark:text-yellow-400">Read-only access</p>
                      <p className="mt-0.5">
                        Aurora can monitor and diagnose but cannot take remediation actions (restart, stop, start machines).
                        To enable remediation, reconnect with a full org token from your{" "}
                        <a href={`https://fly.io/dashboard/${status.org_slug}/tokens`} target="_blank" rel="noopener noreferrer" className="underline">dashboard tokens page</a>.
                      </p>
                    </div>
                  </div>
                )}

                <div className="flex items-center justify-end pt-2">
                  <Button
                    variant="destructive"
                    onClick={handleDisconnect}
                    disabled={isDisconnecting}
                  >
                    {isDisconnecting ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Disconnecting...
                      </>
                    ) : (
                      <>
                        <LogOut className="mr-2 h-4 w-4" />
                        Disconnect
                      </>
                    )}
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        ) : (
          <Card>
            <CardHeader>
              <CardTitle>Connect Your Fly.io Organization</CardTitle>
              <CardDescription>Generate an org-scoped API token and paste it below</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-4 text-sm">
                <p className="text-muted-foreground">
                  Aurora uses an org-scoped API token to monitor your Fly.io applications. Generate one from your Fly.io dashboard.
                </p>

                <div className="p-4 bg-muted/50 border border-border rounded-lg space-y-3">
                  <p className="font-medium text-foreground">Setup</p>
                  <div className="space-y-2">
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">1.</span>
                      <div>
                        <p>
                          In your Fly.io dashboard, go to <span className="font-medium">Account</span> →{" "}
                          <a href="https://fly.io/tokens" target="_blank" rel="noopener noreferrer" className="text-blue-600 dark:text-blue-400 hover:underline inline-flex items-center gap-1">
                            Access Tokens <ExternalLink className="w-3 h-3" />
                          </a>{" "}
                          and create a token for your organization
                        </p>
                      </div>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">2.</span>
                      <p>Copy the token and paste it below along with your org slug</p>
                    </div>
                  </div>
                  <p className="text-xs text-muted-foreground mt-2">
                    Aurora will automatically detect your token&apos;s permission level (read-only or full access).
                  </p>
                </div>
              </div>

              {error && (
                <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4 flex items-start gap-3">
                  <AlertCircle className="h-5 w-5 text-destructive flex-shrink-0 mt-0.5" />
                  <p className="text-sm text-destructive">{error}</p>
                </div>
              )}

              <form onSubmit={handleConnect} className="space-y-4">
                <div className="grid gap-2">
                  <Label htmlFor="orgSlug">Organization Slug *</Label>
                  <Input
                    id="orgSlug"
                    type="text"
                    placeholder="e.g. personal, my-company"
                    value={orgSlug}
                    onChange={(e) => setOrgSlug(e.target.value)}
                    required
                    disabled={isLoading}
                  />
                  <p className="text-xs text-muted-foreground">
                    Use <code className="bg-muted px-1 py-0.5 rounded">personal</code> for personal accounts, or your org name for team organizations
                  </p>
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="apiToken">API Token *</Label>
                  <Input
                    id="apiToken"
                    type="password"
                    placeholder="Paste your Fly.io org token"
                    value={apiToken}
                    onChange={(e) => setApiToken(e.target.value)}
                    required
                    disabled={isLoading}
                  />
                </div>

                <div className="flex items-center justify-end pt-4">
                  <Button type="submit" disabled={isLoading || !apiToken.trim() || !orgSlug.trim()}>
                    {isLoading ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Connecting...
                      </>
                    ) : (
                      "Connect Fly.io"
                    )}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

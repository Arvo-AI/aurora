"use client";

import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Loader2, ExternalLink, AlertCircle, CheckCircle2, Globe, Shield, LogOut, RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { providerPreferencesService } from '@/lib/services/providerPreferences';
import { getEnv } from '@/lib/env';
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL');

interface CloudflareZone {
  id: string;
  name: string;
  status: string;
  account_name?: string;
  plan?: string;
}

interface CloudflareStatus {
  connected: boolean;
  email?: string;
  accountName?: string;
}

export default function CloudflareAuthPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const [isLoadingZones, setIsLoadingZones] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [apiToken, setApiToken] = useState("");
  const [status, setStatus] = useState<CloudflareStatus | null>(null);
  const [zones, setZones] = useState<CloudflareZone[]>([]);
  const router = useRouter();
  const { toast } = useToast();

  const getUserId = async (): Promise<string | null> => {
    try {
      const response = await fetch("/api/getUserId");
      const data = await response.json();
      return data.userId || null;
    } catch {
      return null;
    }
  };

  const fetchZones = useCallback(async () => {
    setIsLoadingZones(true);
    try {
      const userId = await getUserId();
      if (!userId) return;

      const response = await fetch(`${backendUrl}/cloudflare_api/cloudflare/zones`, {
        headers: { 'X-User-ID': userId },
        credentials: 'include',
      });

      if (response.ok) {
        const data = await response.json();
        setZones(data.zones || []);
      }
    } catch (err) {
      console.error("Failed to fetch zones:", err);
    } finally {
      setIsLoadingZones(false);
    }
  }, []);

  const checkStatus = useCallback(async () => {
    setIsCheckingStatus(true);
    try {
      const userId = await getUserId();
      if (!userId) {
        setStatus({ connected: false });
        return;
      }

      const response = await fetch(`${backendUrl}/cloudflare_api/cloudflare/status`, {
        headers: { 'X-User-ID': userId },
        credentials: 'include',
      });

      if (response.ok) {
        const data = await response.json();
        const isConnected = data.connected === true;
        setStatus({
          connected: isConnected,
          email: data.email,
          accountName: data.accountName,
        });
        if (isConnected) {
          fetchZones();
        }
      } else {
        setStatus({ connected: false });
      }
    } catch {
      setStatus({ connected: false });
    } finally {
      setIsCheckingStatus(false);
    }
  }, [fetchZones]);

  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();

    if (!apiToken) {
      setError("API token is required");
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      const userId = await getUserId();

      if (!userId) {
        toast({
          title: "Authentication Required",
          description: "Please wait for authentication to complete before connecting to Cloudflare.",
          variant: "destructive",
        });
        setIsLoading(false);
        return;
      }

      const response = await fetch(`${backendUrl}/cloudflare_api/cloudflare/connect`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-User-ID': userId,
        },
        body: JSON.stringify({ apiToken }),
        credentials: 'include',
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to connect to Cloudflare');
      }

      localStorage.setItem("isCloudflareConnected", "true");

      await providerPreferencesService.smartAutoSelect('cloudflare', true);

      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      window.dispatchEvent(new CustomEvent('providerConnectionAction'));

      const parts = [];
      if (data.accountCount) parts.push(`${data.accountCount} account(s)`);
      if (data.zonesCount) parts.push(`${data.zonesCount} zone(s)`);
      const summary = parts.length > 0 ? `Found ${parts.join(", ")}.` : "Connected.";

      toast({
        title: "Cloudflare Connected",
        description: `Successfully connected. ${summary}`,
      });

      setApiToken("");
      setStatus({
        connected: true,
        email: data.email,
        accountName: data.accountName,
      });
      setIsLoading(false);
      fetchZones();
    } catch (err: unknown) {
      console.error('Cloudflare connect error:', err);
      const errorMessage = err instanceof Error ? err.message : 'Failed to connect to Cloudflare';
      setError(errorMessage);
      setIsLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      const userId = await getUserId();
      if (!userId) return;

      const response = await fetch(`${backendUrl}/cloudflare_api/cloudflare/disconnect`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-User-ID': userId,
        },
        credentials: 'include',
      });

      if (response.ok) {
        localStorage.removeItem("isCloudflareConnected");
        window.dispatchEvent(new CustomEvent('providerStateChanged'));

        toast({
          title: "Cloudflare Disconnected",
          description: "Your Cloudflare account has been disconnected.",
        });

        setStatus({ connected: false });
        setZones([]);
        setApiToken("");
      } else {
        const data = await response.json();
        throw new Error(data.error || "Failed to disconnect");
      }
    } catch (err: unknown) {
      console.error("Cloudflare disconnect error:", err);
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : "Failed to disconnect Cloudflare",
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
    }
  };

  const statusColor: Record<string, string> = {
    active: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
    pending: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
    moved: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-400",
  };

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="Cloudflare">
        <div className="container mx-auto py-8 px-4 max-w-3xl flex justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      </ConnectorAuthGuard>
    );
  }

  return (
    <ConnectorAuthGuard connectorName="Cloudflare">
      <div className="container mx-auto py-8 px-4 max-w-3xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Cloudflare Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect to Cloudflare for DNS management, cache purging, security rules, traffic analytics, and Workers monitoring.
          </p>
        </div>

        {status?.connected ? (
          /* ---- Connected state ---- */
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <CheckCircle2 className="h-5 w-5 text-green-500" />
                    <div>
                      <CardTitle>Cloudflare Connected</CardTitle>
                      <CardDescription>Your Cloudflare account is linked to Aurora</CardDescription>
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={fetchZones}
                    disabled={isLoadingZones}
                    className="text-muted-foreground"
                  >
                    <RefreshCw className={`h-4 w-4 ${isLoadingZones ? 'animate-spin' : ''}`} />
                  </Button>
                </div>
              </CardHeader>
              <CardContent className="space-y-5">
                <div className="grid gap-3 sm:grid-cols-2">
                  {status.accountName && (
                    <div className="flex items-center gap-3 p-3 bg-muted/50 rounded-lg">
                      <Shield className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                      <div className="min-w-0">
                        <p className="text-xs text-muted-foreground">Account</p>
                        <p className="text-sm font-medium truncate">{status.accountName}</p>
                      </div>
                    </div>
                  )}
                  {status.email && (
                    <div className="flex items-center gap-3 p-3 bg-muted/50 rounded-lg">
                      <Globe className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                      <div className="min-w-0">
                        <p className="text-xs text-muted-foreground">Email</p>
                        <p className="text-sm font-medium truncate">{status.email}</p>
                      </div>
                    </div>
                  )}
                </div>

                {/* Zones list */}
                {isLoadingZones ? (
                  <div className="flex items-center gap-2 py-4 justify-center text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span className="text-sm">Loading zones...</span>
                  </div>
                ) : zones.length > 0 ? (
                  <div>
                    <p className="text-sm font-medium mb-2">Zones ({zones.length})</p>
                    <div className="border rounded-lg divide-y">
                      {zones.map((zone) => (
                        <div key={zone.id} className="flex items-center justify-between px-4 py-2.5">
                          <div className="flex items-center gap-2 min-w-0">
                            <span className="text-sm font-medium truncate">{zone.name}</span>
                            {zone.plan && (
                              <span className="text-xs text-muted-foreground hidden sm:inline">
                                {zone.plan}
                              </span>
                            )}
                          </div>
                          <Badge
                            variant="secondary"
                            className={`text-xs shrink-0 ${statusColor[zone.status] || ""}`}
                          >
                            {zone.status}
                          </Badge>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="text-center py-4 text-sm text-muted-foreground">
                    No zones found. The token may not have zone-level permissions.
                  </div>
                )}

                <div className="p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded text-xs text-muted-foreground">
                  No webhook required — Aurora queries the Cloudflare API on demand. To change token permissions, edit the token in the{" "}
                  <a
                    href="https://dash.cloudflare.com/profile/api-tokens"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-600 dark:text-blue-400 hover:underline inline-flex items-center gap-1"
                  >
                    Cloudflare Dashboard
                    <ExternalLink className="w-3 h-3" />
                  </a>.
                </div>

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
          /* ---- Not connected state ---- */
          <>
            <Card>
              <CardHeader>
                <CardTitle>Connect Your Cloudflare Account</CardTitle>
                <CardDescription>Create an API token in the Cloudflare dashboard and paste it below</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="space-y-3 text-sm">
                  <p className="text-muted-foreground">
                    Aurora uses a scoped API token to interact with your Cloudflare account. The token&apos;s
                    permissions determine what Aurora can do — from read-only monitoring to active incident response.
                  </p>

                  <div className="space-y-2">
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">1.</span>
                      <p>Go to the <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">Cloudflare API Tokens</a> page</p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">2.</span>
                      <p>Click <strong>&quot;Create Token&quot;</strong></p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">3.</span>
                      <p>Select <strong>&quot;Create Custom Token&quot;</strong> and add permissions based on your needs (see below)</p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">4.</span>
                      <p>Under <strong>&quot;Zone Resources&quot;</strong>, select the zones (domains) you want Aurora to access</p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">5.</span>
                      <p>Click <strong>&quot;Continue to summary&quot;</strong> → <strong>&quot;Create Token&quot;</strong></p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">6.</span>
                      <p>Copy the token (it&apos;s only shown once)</p>
                    </div>
                  </div>

                  <div className="mt-4 space-y-3">
                    <p className="font-medium text-foreground">Recommended permissions:</p>
                    <div className="space-y-2">
                      <div className="p-3 bg-muted/50 border border-border rounded">
                        <p className="font-medium text-xs text-foreground mb-1">Monitoring (read-only)</p>
                        <p className="text-xs text-muted-foreground">
                          Zone — Read, Analytics — Read, DNS — Read, Firewall — Read, Load Balancers — Read, Workers — Read
                        </p>
                      </div>
                      <div className="p-3 bg-muted/50 border border-border rounded">
                        <p className="font-medium text-xs text-foreground mb-1">Incident response (read + write)</p>
                        <p className="text-xs text-muted-foreground">
                          All of the above, plus: DNS — Edit, Cache Purge — Purge, Firewall — Edit, Workers — Edit, Page Rules — Edit, Load Balancers — Edit
                        </p>
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      You can start with read-only and update the token permissions later in the Cloudflare dashboard.
                    </p>
                  </div>

                  <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
                    <a
                      href="https://dash.cloudflare.com/profile/api-tokens"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
                    >
                      Open Cloudflare Dashboard
                      <ExternalLink className="w-3 h-3" />
                    </a>
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
                    <Label htmlFor="apiToken">API Token *</Label>
                    <Input
                      id="apiToken"
                      type="password"
                      placeholder="Paste your Cloudflare API token"
                      value={apiToken}
                      onChange={(e) => setApiToken(e.target.value)}
                      required
                      disabled={isLoading}
                    />
                  </div>

                  <div className="flex items-center justify-end pt-4">
                    <Button type="submit" disabled={isLoading || !apiToken.trim()}>
                      {isLoading ? (
                        <>
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                          Connecting...
                        </>
                      ) : (
                        "Connect Cloudflare"
                      )}
                    </Button>
                  </div>
                </form>
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

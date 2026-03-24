"use client";

import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Loader2, ExternalLink, AlertCircle, CheckCircle2, Globe, Shield, LogOut, ChevronDown, ChevronUp } from "lucide-react";
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
  permissions?: string[];
  tokenType?: "account" | "user";
}

const REQUIRED_READ_PERMISSIONS: Record<string, string> = {
  "Zone Read": "Zone — Zone — Read",
  "DNS Read": "Zone — DNS — Read",
  "Analytics Read": "Zone — Analytics — Read",
  "Firewall Services Read": "Zone — Firewall Services — Read",
  "Load Balancers Account Read": "Account — Load Balancing: Account Load Balancers — Read",
  "Account API Tokens Read": "Account — API Tokens — Read",
};

const RECOMMENDED_WRITE_PERMISSIONS: Record<string, string> = {
  "DNS Write": "Zone — DNS — Edit",
  "Cache Purge": "Zone — Cache Purge — Purge",
  "Firewall Services Write": "Zone — Firewall Services — Edit",
  "Load Balancers Account Write": "Account — Load Balancing: Account Load Balancers — Edit",
};

function PermissionStatus({ permissions }: { permissions: string[] }) {
  const [showAll, setShowAll] = useState(false);
  const permSet = new Set(permissions);

  const missingReadEntries = Object.entries(REQUIRED_READ_PERMISSIONS).filter(
    ([key]) => !permSet.has(key)
  );

  const grantedWriteKeys = Object.keys(RECOMMENDED_WRITE_PERMISSIONS).filter(key => permSet.has(key));
  const missingWriteEntries = Object.entries(RECOMMENDED_WRITE_PERMISSIONS).filter(
    ([key]) => !permSet.has(key)
  );
  const hasWrite = grantedWriteKeys.length > 0;
  const hasAllWrite = missingWriteEntries.length === 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {hasAllWrite && missingReadEntries.length === 0 ? (
          <>
            <CheckCircle2 className="h-4 w-4 text-green-500" />
            <span className="text-sm font-medium">Full access</span>
          </>
        ) : hasWrite ? (
          <>
            <CheckCircle2 className="h-4 w-4 text-green-500" />
            <span className="text-sm font-medium">Read + partial write</span>
          </>
        ) : (
          <>
            <Shield className="h-4 w-4 text-yellow-500" />
            <span className="text-sm font-medium">Read-only</span>
          </>
        )}
      </div>

      {missingReadEntries.length > 0 && (
        <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg space-y-2">
          <p className="text-xs font-medium text-red-600 dark:text-red-400">Missing required read permissions:</p>
          <ul className="text-xs text-muted-foreground list-disc list-inside space-y-0.5">
            {missingReadEntries.map(([, cfName]) => (
              <li key={cfName}>{cfName}</li>
            ))}
          </ul>
        </div>
      )}

      {grantedWriteKeys.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">Write permissions granted:</p>
          {grantedWriteKeys.map(key => (
            <div key={key} className="flex items-center gap-2">
              <CheckCircle2 className="h-3.5 w-3.5 text-green-500 flex-shrink-0" />
              <span className="text-xs">{key}</span>
            </div>
          ))}
        </div>
      )}

      {missingWriteEntries.length > 0 && (
        <div className="p-3 bg-muted/50 rounded-lg space-y-2">
          <p className="text-xs text-muted-foreground">
            {hasWrite
              ? "Missing write permissions — add for full control:"
              : "To let Aurora take action during incidents, edit the token and add:"}
          </p>
          <ul className="text-xs text-muted-foreground list-disc list-inside space-y-0.5">
            {missingWriteEntries.map(([, cfName]) => (
              <li key={cfName}>{cfName}</li>
            ))}
          </ul>
        </div>
      )}

      <button
        type="button"
        onClick={() => setShowAll(!showAll)}
        className="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
      >
        {showAll ? "Hide" : "Show"} all {permissions.length} permissions
        {showAll ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>

      {showAll && (
        <div className="border rounded-lg divide-y max-h-48 overflow-y-auto">
          {permissions.map(p => (
            <div key={p} className="px-3 py-1.5 text-xs text-muted-foreground">{p}</div>
          ))}
        </div>
      )}
    </div>
  );
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
          permissions: data.permissions,
          tokenType: data.tokenType,
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

      toast({
        title: "Cloudflare Connected",
        description: data.zonesCount
          ? `Successfully connected. Found ${data.zonesCount} zone(s).`
          : "Successfully connected.",
      });

      setApiToken("");
      setStatus({
        connected: true,
        email: data.email,
        accountName: data.accountName,
        permissions: data.permissions,
        tokenType: data.tokenType,
      });
      fetchZones();
    } catch (err: unknown) {
      console.error('Cloudflare connect error:', err);
      const errorMessage = err instanceof Error ? err.message : 'Failed to connect to Cloudflare';
      setError(errorMessage);
    } finally {
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
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <div className="flex items-center gap-3">
                  <CheckCircle2 className="h-5 w-5 text-green-500" />
                  <div>
                    <CardTitle>Cloudflare Connected</CardTitle>
                    <CardDescription>Your Cloudflare account is linked to Aurora</CardDescription>
                  </div>
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

                {status.tokenType === "user" && (
                  <div className="p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-lg flex items-start gap-2.5">
                    <AlertCircle className="h-4 w-4 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
                    <div className="text-xs text-muted-foreground">
                      <p className="font-medium text-yellow-600 dark:text-yellow-400">Using a personal token</p>
                      <p className="mt-0.5">
                        This token is tied to an individual user account. If that user leaves the organization, Aurora will lose access.
                        For a more durable setup, use an <strong>Account API Token</strong> instead (Manage Account → Account API Tokens).
                      </p>
                    </div>
                  </div>
                )}

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

                {status.permissions && status.permissions.length > 0 ? (
                  <div>
                    <p className="text-sm font-medium mb-2">Token Permissions</p>
                    <PermissionStatus permissions={status.permissions} />
                    <p className="text-xs text-muted-foreground mt-3">
                      Edit the token in the{" "}
                      <a
                        href="https://dash.cloudflare.com/profile/api-tokens"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        Cloudflare Dashboard
                      </a>{" "}and refresh the page to update.
                    </p>
                  </div>
                ) : (
                  <div className="text-sm text-muted-foreground p-3 bg-muted/50 rounded-lg">
                    <p>
                      Token permissions not available. To see them here, edit the token in the{" "}
                      <a
                        href="https://dash.cloudflare.com/profile/api-tokens"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        Cloudflare Dashboard
                      </a>, add <strong>Account — API Tokens — Read</strong>, then reconnect.
                    </p>
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
              <CardTitle>Connect Your Cloudflare Account</CardTitle>
              <CardDescription>Create an API token in the Cloudflare dashboard and paste it below</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-4 text-sm">
                <p className="text-muted-foreground">
                  Aurora uses a scoped API token to interact with your Cloudflare account. This must be set up by an account administrator.
                </p>

                <div className="p-4 bg-muted/50 border border-border rounded-lg space-y-3">
                  <p className="font-medium text-foreground">Setup</p>
                  <div className="space-y-2">
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">1.</span>
                      <div>
                        <p>
                          In the{" "}
                          <a href="https://dash.cloudflare.com" target="_blank" rel="noopener noreferrer" className="text-blue-600 dark:text-blue-400 hover:underline inline-flex items-center gap-1">
                            Cloudflare Dashboard <ExternalLink className="w-3 h-3" />
                          </a>
                          , go to <strong>Manage Account</strong> → <strong>Account API Tokens</strong>
                        </p>
                      </div>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">2.</span>
                      <p>Next to <strong>&quot;Read all resources&quot;</strong>, click <strong>&quot;Use template&quot;</strong></p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">3.</span>
                      <p>Click <strong>&quot;Create Token&quot;</strong></p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">4.</span>
                      <p>Under <strong>Permissions</strong>, click <strong>&quot;+ Add more&quot;</strong> and add <strong>Account — API Tokens — Read</strong></p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">5.</span>
                      <p>Select your zone(s) under <strong>Zone Resources</strong></p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">6.</span>
                      <div>
                        <p><em>Optional</em> — Under <strong>Permissions</strong>, also add these to let Aurora take action during incidents:</p>
                        <ul className="text-xs text-muted-foreground mt-1 list-disc list-inside space-y-0.5">
                          <li>Zone — DNS — Edit</li>
                          <li>Zone — Cache Purge — Purge</li>
                          <li>Zone — Firewall Services — Edit</li>
                          <li>Account — Load Balancing: Account Load Balancers — Edit</li>
                        </ul>
                      </div>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">7.</span>
                      <p>Click <strong>&quot;Continue to summary&quot;</strong> → <strong>&quot;Create Token&quot;</strong></p>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-muted-foreground mt-0.5">8.</span>
                      <p>Copy the token and paste it below</p>
                    </div>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Requires <strong>Super Administrator</strong> permission. You can start read-only and add write permissions later.
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
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

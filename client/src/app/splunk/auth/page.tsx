"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { splunkService, SplunkStatus } from "@/lib/services/splunk";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2, ExternalLink } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { SplunkWebhookStep } from "@/components/splunk/SplunkWebhookStep";

const CACHE_KEY = "splunk_connection_status";

export default function SplunkAuthPage() {
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [status, setStatus] = useState<SplunkStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);

  const loadStatus = async (skipCache = false) => {
    try {
      // Show cached status immediately for fast UX
      if (!skipCache && typeof window !== "undefined") {
        const cachedStatus = localStorage.getItem(CACHE_KEY);
        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus);
          setStatus(parsedStatus);
          setIsCheckingStatus(false);
          if (parsedStatus?.connected) {
            setBaseUrl(parsedStatus.baseUrl ?? "");
          }
        }
      }
      // Always verify with API in background (handles expired tokens)
      await fetchAndUpdateStatus();
    } catch (err) {
      console.error("Failed to load Splunk status", err);
      setIsCheckingStatus(false);
    }
  };

  const fetchAndUpdateStatus = async () => {
    try {
      const result = await splunkService.getStatus();
      if (result !== null) {
        const cachedStatus = localStorage.getItem(CACHE_KEY);
        const wasCachedConnected = cachedStatus ? JSON.parse(cachedStatus)?.connected : false;
        const stateChanged = wasCachedConnected !== result.connected;

        setStatus(result);
        if (typeof window !== "undefined") {
          localStorage.setItem(CACHE_KEY, JSON.stringify(result));
          if (result.connected) {
            localStorage.setItem("isSplunkConnected", "true");
          } else {
            localStorage.removeItem("isSplunkConnected");
          }
          if (stateChanged) {
            window.dispatchEvent(new CustomEvent("providerStateChanged"));
          }
        }
        if (result.connected) {
          setBaseUrl(result.baseUrl ?? "");
        }
      }
    } catch (err) {
      console.error("[Splunk] Failed to fetch status:", err);
    } finally {
      setIsCheckingStatus(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = { baseUrl, apiToken };
      const result = await splunkService.connect(payload);
      setStatus(result);

      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
      }

      toast({
        title: "Success",
        description: "Splunk connected successfully!",
      });

      if (typeof window !== "undefined") {
        localStorage.setItem("isSplunkConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    } catch (err: any) {
      console.error("Splunk connection failed", err);
      toast({
        title: "Failed to connect to Splunk",
        description: getUserFriendlyError(err),
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
      const response = await fetch("/api/connected-accounts/splunk", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setBaseUrl("");

        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEY);
          localStorage.removeItem("isSplunkConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }

        toast({
          title: "Success",
          description: "Splunk disconnected successfully",
        });
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Splunk");
      }
    } catch (err: any) {
      console.error("Splunk disconnect failed", err);
      toast({
        title: "Failed to disconnect Splunk",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  if (isCheckingStatus) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Splunk Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect your Splunk Cloud or Enterprise instance
          </p>
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
        <h1 className="text-3xl font-bold">Splunk Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect your Splunk Cloud or Enterprise instance
        </p>
      </div>

      {!status?.connected ? (
        <Card>
          <CardHeader>
            <CardTitle>Connect to Splunk</CardTitle>
            <CardDescription>
              Enter your Splunk instance URL and API token to establish a connection.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleConnect} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="baseUrl">Instance URL</Label>
                <Input
                  id="baseUrl"
                  type="url"
                  placeholder="https://your-splunk-instance:8089"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Include the REST API port (default: <code className="bg-muted px-1 rounded">8089</code>)
                </p>
              </div>

              <div className="bg-muted/50 rounded-lg p-3 text-xs space-y-1">
                <p className="font-medium">Example URLs:</p>
                <ul className="text-muted-foreground space-y-0.5">
                  <li><strong>Enterprise:</strong> https://splunk.yourcompany.com:8089</li>
                  <li><strong>Cloud:</strong> https://your-stack.splunkcloud.com:8089</li>
                  <li><strong>Local:</strong> https://host.docker.internal:8089</li>
                </ul>
                <p className="mt-2 text-muted-foreground/80">
                  <strong>Note:</strong> Aurora must have network access to your Splunk instance.
                  If Splunk is behind a VPN or firewall, ensure port 8089 is reachable from Aurora.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="apiToken">API Token</Label>
                <Input
                  id="apiToken"
                  type="password"
                  placeholder="Enter your Splunk API token"
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Create a token in Splunk: Settings → Tokens → New Token
                </p>
              </div>

              <div className="bg-muted/50 rounded-lg p-4 text-sm">
                <p className="font-medium mb-2">How to get your API token:</p>
                <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                  <li>Log in to your Splunk instance</li>
                  <li>Go to Settings → Tokens</li>
                  <li>Click &quot;New Token&quot;</li>
                  <li>Set an expiration and create the token</li>
                  <li>Copy and paste the token above</li>
                </ol>
                <a
                  href="https://docs.splunk.com/Documentation/SplunkCloud/latest/Security/CreateAuthTokens"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2"
                >
                  View Splunk documentation <ExternalLink className="h-3 w-3" />
                </a>
              </div>

              <Button type="submit" className="w-full" disabled={loading || !baseUrl || !apiToken}>
                {loading ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Connecting...
                  </>
                ) : (
                  "Connect to Splunk"
                )}
              </Button>
            </form>
          </CardContent>
        </Card>
      ) : (
        <SplunkWebhookStep
          status={status}
          onDisconnect={handleDisconnect}
          loading={loading}
        />
      )}
    </div>
  );
}

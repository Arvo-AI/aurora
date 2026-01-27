"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { confluenceService, ConfluenceStatus } from "@/lib/services/confluence";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function ConfluenceConnectPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<ConfluenceStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [patBaseUrl, setPatBaseUrl] = useState("");
  const [patToken, setPatToken] = useState("");
  const [isOauthConnecting, setIsOauthConnecting] = useState(false);
  const [isPatConnecting, setIsPatConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);

  const STATUS_TIMEOUT_MS = 12000;

  const loadStatus = async (stateRef: { active: boolean }) => {
    setIsLoading(true);
    let didTimeout = false;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => {
      didTimeout = true;
      controller.abort();
      if (!stateRef.active) {
        return;
      }
      const cachedConnected = localStorage.getItem("isConfluenceConnected") === "true";
      if (cachedConnected) {
        setStatus((prev) => (prev?.connected ? prev : { connected: true }));
      }
      setIsLoading(false);
      toast({
        title: "Confluence status delayed",
        description: "Using cached connection status.",
      });
    }, STATUS_TIMEOUT_MS);
    try {
      const result = await confluenceService.getStatus({ signal: controller.signal });
      if (!stateRef.active || didTimeout) {
        return;
      }
      setStatus(result);
      if (result?.connected) {
        localStorage.setItem("isConfluenceConnected", "true");
      } else {
        localStorage.removeItem("isConfluenceConnected");
      }
      if (result?.connected && result.baseUrl) {
        setPatBaseUrl(result.baseUrl);
      }
    } catch (err) {
      if (!stateRef.active || didTimeout) {
        return;
      }
      if (err instanceof DOMException && err.name === "AbortError") {
        return;
      }
      console.error("Failed to load Confluence status", err);
      const cachedConnected = localStorage.getItem("isConfluenceConnected") === "true";
      if (cachedConnected) {
        setStatus((prev) => (prev?.connected ? prev : { connected: true }));
      }
    } finally {
      clearTimeout(timeoutId);
      if (stateRef.active && !didTimeout) {
        setIsLoading(false);
      }
    }
  };

  useEffect(() => {
    const stateRef = { active: true };
    loadStatus(stateRef);
    return () => {
      stateRef.active = false;
    };
  }, []);

  const handleOAuthConnect = async () => {
    setIsOauthConnecting(true);
    try {
      const result = await confluenceService.connect({ authType: "oauth" });

      if (result?.authUrl) {
        window.location.href = result.authUrl;
        return;
      }

      if (result?.connected) {
        setStatus(result);
        toast({ title: "Confluence connected", description: "OAuth connection established." });
        localStorage.setItem("isConfluenceConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      } else {
        throw new Error("Unable to start Confluence OAuth flow.");
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "OAuth connection failed";
      toast({ title: "Failed to connect Confluence", description: message, variant: "destructive" });
    } finally {
      setIsOauthConnecting(false);
    }
  };

  const handlePatConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIsPatConnecting(true);

    try {
      const result = await confluenceService.connect({
        authType: "pat",
        baseUrl: patBaseUrl,
        patToken,
      });
      setStatus(result);
      toast({ title: "Confluence connected", description: "PAT connection established." });
      localStorage.setItem("isConfluenceConnected", "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
      setPatToken("");
    } catch (err) {
      const message = err instanceof Error ? err.message : "PAT connection failed";
      toast({ title: "Failed to connect Confluence", description: message, variant: "destructive" });
    } finally {
      setIsPatConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      await confluenceService.disconnect();
      setStatus({ connected: false });
      toast({ title: "Confluence disconnected" });
      localStorage.removeItem("isConfluenceConnected");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Disconnect failed";
      toast({ title: "Failed to disconnect Confluence", description: message, variant: "destructive" });
    } finally {
      setIsDisconnecting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-3xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Confluence Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect Confluence to fetch runbooks and documentation
          </p>
        </div>
        <Card>
          <CardContent className="flex items-center justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-3xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Confluence Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect Confluence to fetch runbooks and documentation
        </p>
      </div>

      {status?.connected ? (
        <Card>
          <CardHeader>
            <CardTitle>Confluence Connected</CardTitle>
            <CardDescription>
              Your Confluence workspace is connected and ready for runbook ingestion.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div><span className="font-medium">Base URL:</span> {status.baseUrl || "Unknown"}</div>
            <div><span className="font-medium">Auth Type:</span> {status.authType || "oauth"}</div>
          </CardContent>
          <CardFooter>
            <Button variant="destructive" onClick={handleDisconnect} disabled={isDisconnecting}>
              {isDisconnecting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Disconnecting...
                </>
              ) : (
                "Disconnect Confluence"
              )}
            </Button>
          </CardFooter>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Confluence Cloud (OAuth)</CardTitle>
              <CardDescription>
                Connect your Atlassian Cloud site using OAuth 2.0.
              </CardDescription>
            </CardHeader>
            <CardFooter>
              <Button onClick={handleOAuthConnect} disabled={isOauthConnecting}>
                {isOauthConnecting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Redirecting...
                  </>
                ) : (
                  "Connect with Atlassian"
                )}
              </Button>
            </CardFooter>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Confluence Data Center (PAT)</CardTitle>
              <CardDescription>
                Connect a self-hosted Confluence instance using a personal access token.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handlePatConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="patBaseUrl">Base URL</Label>
                  <Input
                    id="patBaseUrl"
                    type="url"
                    placeholder="https://confluence.internal"
                    value={patBaseUrl}
                    onChange={(e) => setPatBaseUrl(e.target.value)}
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="patToken">Personal Access Token</Label>
                  <Input
                    id="patToken"
                    type="password"
                    placeholder="Enter your Confluence PAT"
                    value={patToken}
                    onChange={(e) => setPatToken(e.target.value)}
                    required
                  />
                </div>
                <Button type="submit" disabled={isPatConnecting || !patBaseUrl || !patToken} className="w-full">
                  {isPatConnecting ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Connecting...
                    </>
                  ) : (
                    "Connect with PAT"
                  )}
                </Button>
              </form>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { sharepointService, SharePointStatus } from "@/lib/services/sharepoint";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";

export default function SharePointConnectPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<SharePointStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isOauthConnecting, setIsOauthConnecting] = useState(false);
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
      const cachedConnected = localStorage.getItem("isSharePointConnected") === "true";
      if (cachedConnected) {
        setStatus((prev) => (prev?.connected ? prev : { connected: true }));
      }
      setIsLoading(false);
      toast({
        title: "SharePoint status delayed",
        description: "Using cached connection status.",
      });
    }, STATUS_TIMEOUT_MS);
    try {
      const result = await sharepointService.getStatus({ signal: controller.signal });
      if (!stateRef.active || didTimeout) {
        return;
      }
      setStatus(result);
      if (result?.connected) {
        localStorage.setItem("isSharePointConnected", "true");
      } else {
        localStorage.removeItem("isSharePointConnected");
      }
    } catch (err) {
      if (!stateRef.active || didTimeout) {
        return;
      }
      if (err instanceof DOMException && err.name === "AbortError") {
        return;
      }
      console.error("Failed to load SharePoint status", err);
      const cachedConnected = localStorage.getItem("isSharePointConnected") === "true";
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
    let redirecting = false;
    try {
      const result = await sharepointService.connect({});

      if (result?.authUrl) {
        redirecting = true;
        window.location.href = result.authUrl;
        return;
      }

      if (result?.connected) {
        setStatus(result);
        toast({ title: "SharePoint connected", description: "OAuth connection established." });
        localStorage.setItem("isSharePointConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      } else {
        throw new Error("Unable to start SharePoint OAuth flow.");
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "OAuth connection failed";
      toast({ title: "Failed to connect SharePoint", description: message, variant: "destructive" });
    } finally {
      if (!redirecting) {
        setIsOauthConnecting(false);
      }
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      await sharepointService.disconnect();
      setStatus({ connected: false });
      toast({ title: "SharePoint disconnected" });
      localStorage.removeItem("isSharePointConnected");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Disconnect failed";
      toast({ title: "Failed to disconnect SharePoint", description: message, variant: "destructive" });
    } finally {
      setIsDisconnecting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-3xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">SharePoint Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect SharePoint to fetch documents and site pages
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
        <h1 className="text-3xl font-bold">SharePoint Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect SharePoint to fetch documents and site pages
        </p>
      </div>

      {status?.connected ? (
        <Card>
          <CardHeader>
            <CardTitle>SharePoint Connected</CardTitle>
            <CardDescription>
              Your SharePoint site is connected and ready for document ingestion.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {status.userDisplayName && (
              <div><span className="font-medium">User:</span> {status.userDisplayName}</div>
            )}
            {status.userEmail && (
              <div><span className="font-medium">Email:</span> {status.userEmail}</div>
            )}
          </CardContent>
          <CardFooter>
            <Button variant="destructive" onClick={handleDisconnect} disabled={isDisconnecting}>
              {isDisconnecting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Disconnecting...
                </>
              ) : (
                "Disconnect SharePoint"
              )}
            </Button>
          </CardFooter>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Microsoft SharePoint (OAuth)</CardTitle>
            <CardDescription>
              Connect your SharePoint Online site using Microsoft OAuth 2.0.
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
                "Connect with Microsoft"
              )}
            </Button>
          </CardFooter>
        </Card>
      )}
    </div>
  );
}

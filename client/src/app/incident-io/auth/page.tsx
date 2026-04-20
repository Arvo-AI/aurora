"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { incidentIoService, IncidentIoStatus } from "@/lib/services/incident-io";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { IncidentIoWebhookStep } from "@/components/incident-io/IncidentIoWebhookStep";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEY = "incident_io_connection_status";

export default function IncidentIoAuthPage() {
  const { toast } = useToast();
  const [apiKey, setApiKey] = useState("");
  const [status, setStatus] = useState<IncidentIoStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== "undefined") {
        const cachedStatus = localStorage.getItem(CACHE_KEY);
        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus);
          setStatus(parsedStatus);
          setIsCheckingStatus(false);
          if (parsedStatus?.connected) return;
        }
      }
      await fetchAndUpdateStatus();
    } catch (err) {
      console.error("Failed to load incident.io status", err);
      setIsCheckingStatus(false);
    }
  };

  const fetchAndUpdateStatus = async () => {
    try {
      const result = await incidentIoService.getStatus();
      if (result !== null) {
        const cachedStatus = localStorage.getItem(CACHE_KEY);
        const wasCachedConnected = cachedStatus ? JSON.parse(cachedStatus)?.connected : false;
        const stateChanged = wasCachedConnected !== result.connected;

        setStatus(result);
        if (typeof window !== "undefined") {
          localStorage.setItem(CACHE_KEY, JSON.stringify(result));
          if (result.connected) {
            localStorage.setItem("isIncidentIoConnected", "true");
          } else {
            localStorage.removeItem("isIncidentIoConnected");
          }
          if (stateChanged) {
            window.dispatchEvent(new CustomEvent("providerStateChanged"));
          }
        }
      }
    } catch (err) {
      console.error("[incident.io] Failed to fetch status:", err);
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
      const result = await incidentIoService.connect({ apiKey });
      setStatus(result);

      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        localStorage.setItem("isIncidentIoConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }

      toast({
        title: "Success",
        description: "incident.io connected successfully!",
      });
    } catch (err: any) {
      console.error("incident.io connection failed", err);
      toast({
        title: "Failed to connect to incident.io",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
      setApiKey("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);

    try {
      const response = await fetch("/api/connected-accounts/incidentio", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });

        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEY);
          localStorage.removeItem("isIncidentIoConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }

        toast({
          title: "Success",
          description: "incident.io disconnected successfully",
        });
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect");
      }
    } catch (err: any) {
      console.error("incident.io disconnect failed", err);
      toast({
        title: "Failed to disconnect incident.io",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="incident.io">
        <div className="container mx-auto py-8 px-4 max-w-2xl">
          <div className="mb-6">
            <h1 className="text-3xl font-bold">incident.io Integration</h1>
            <p className="text-muted-foreground mt-1">
              Connect your incident.io account for incident lifecycle tracking
            </p>
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
    <ConnectorAuthGuard connectorName="incident.io">
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">incident.io Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect your incident.io account for incident lifecycle tracking
          </p>
        </div>

        {!status?.connected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect to incident.io</CardTitle>
              <CardDescription>
                Enter your incident.io API key to establish a connection.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleConnect} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="apiKey">API Key</Label>
                  <Input
                    id="apiKey"
                    type="password"
                    placeholder="Enter your incident.io API key"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    required
                  />
                </div>

                <div className="bg-muted/50 rounded-lg p-4 text-sm">
                  <p className="font-medium mb-2">How to get your API key:</p>
                  <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                    <li>Go to <strong className="text-foreground">incident.io</strong> &rarr; <strong className="text-foreground">Settings</strong> &rarr; <strong className="text-foreground">API keys</strong></li>
                    <li>Click <strong className="text-foreground">Create API key</strong></li>
                    <li>Give it a descriptive name (e.g. &quot;Aurora RCA&quot;)</li>
                    <li>Select permissions: <code className="bg-muted px-1 rounded text-xs">read</code> access to incidents and incident updates</li>
                    <li>Copy and paste the key above</li>
                  </ol>
                </div>

                <Button type="submit" className="w-full" disabled={loading || !apiKey}>
                  {loading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Connecting...
                    </>
                  ) : (
                    "Connect to incident.io"
                  )}
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : (
          <IncidentIoWebhookStep
            onDisconnect={handleDisconnect}
            loading={loading}
          />
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

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
import Image from "next/image";

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

  const isConnected = Boolean(status?.connected);

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="incident.io">
        <div className="container mx-auto py-8 px-4 max-w-2xl">
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
        <div className="flex items-center gap-4 mb-6">
          <div className="flex items-center justify-center h-12 w-12 rounded-lg bg-white dark:bg-white p-1.5">
            <Image src="/incidentio.svg" alt="incident.io" width={40} height={40} />
          </div>
          <div>
            <h1 className="text-3xl font-bold">incident.io</h1>
            <p className="text-muted-foreground mt-0.5">
              Incident lifecycle tracking and automated RCA
            </p>
          </div>
        </div>

        <div className="flex items-center justify-center mb-8">
          <div className="flex items-center">
            <div className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${!isConnected ? 'text-white' : 'bg-gray-200 text-gray-600'}`} style={!isConnected ? { backgroundColor: '#F04438' } : undefined}>
              1
            </div>
            <div className="w-24 h-1" style={{ backgroundColor: isConnected ? '#F04438' : '#e5e7eb' }}></div>
            <div className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${isConnected ? 'text-white' : 'bg-gray-200 text-gray-600'}`} style={isConnected ? { backgroundColor: '#F04438' } : undefined}>
              2
            </div>
          </div>
        </div>

        <div className="flex items-center justify-center mb-6 text-sm font-medium">
          <span className={!isConnected ? 'text-foreground' : 'text-muted-foreground'} style={!isConnected ? { color: '#F04438' } : undefined}>
            Connect
          </span>
          <span className="mx-4 text-muted-foreground">&rarr;</span>
          <span className={isConnected ? 'text-foreground' : 'text-muted-foreground'} style={isConnected ? { color: '#F04438' } : undefined}>
            Configure Webhook
          </span>
        </div>

        {!isConnected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect to incident.io</CardTitle>
              <CardDescription>
                Create an API key at <strong>Settings &rarr; API keys</strong> in incident.io, then paste it below.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form className="space-y-4" onSubmit={handleConnect}>
                <div className="space-y-2">
                  <Label htmlFor="apiKey">API Key</Label>
                  <Input
                    id="apiKey"
                    type="password"
                    placeholder="Paste your incident.io API key"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    The API key needs these permissions: <strong>View all incident data (including private incidents)</strong> and <strong>Create incidents</strong>. Keys are stored securely in Vault.
                  </p>
                </div>

                <Button type="submit" className="w-full" disabled={loading || !apiKey}>
                  {loading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Connecting...
                    </>
                  ) : (
                    "Connect incident.io"
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

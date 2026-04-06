"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { lokiService, LokiStatus } from "@/lib/services/loki";
import { LokiConnectionStep } from "@/components/loki/LokiConnectionStep";
import { LokiWebhookStep } from "@/components/loki/LokiWebhookStep";
import { Button } from "@/components/ui/button";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { copyToClipboard } from "@/lib/utils";

export default function LokiAuthPage() {
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [authType, setAuthType] = useState<"bearer" | "basic" | "none">("bearer");
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [status, setStatus] = useState<LokiStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [instructions, setInstructions] = useState<string[]>([]);
  const [webhookLoading, setWebhookLoading] = useState(false);
  const [webhookError, setWebhookError] = useState<string | null>(null);
  const [copiedField, setCopiedField] = useState<"url" | null>(null);

  const loadStatus = async () => {
    try {
      const result = await lokiService.getStatus();
      setStatus(result);
      if (result?.connected) {
        await loadWebhookUrl();
      }
    } catch (err) {
      console.error("Failed to load Loki status", err);
    } finally {
      setIsInitialLoading(false);
    }
  };

  const loadWebhookUrl = async () => {
    setWebhookLoading(true);
    setWebhookError(null);
    try {
      const response = await lokiService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      setInstructions(response.instructions);
    } catch (err) {
      console.error("Failed to load webhook URL", err);
      const message = err instanceof Error ? err.message : "Failed to load webhook URL";
      setWebhookError(message);
      toast({
        title: "Error loading webhook configuration, please try again.",
        description: message,
        variant: "destructive",
      });
    } finally {
      setWebhookLoading(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = {
        baseUrl,
        authType,
        ...(authType === "bearer" ? { token } : {}),
        ...(authType === "basic" ? { username, password } : {}),
        ...(tenantId ? { tenantId } : {}),
      };
      const result = await lokiService.connect(payload);
      setStatus(result);

      toast({
        title: "Success",
        description: "Loki connected! Now configure the webhook below.",
      });

      await loadWebhookUrl();

      if (typeof window !== "undefined") {
        localStorage.setItem("isLokiConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    } catch (err: unknown) {
      console.error("Loki connection failed", err);
      const message = err instanceof Error ? err.message : "Connection failed";
      toast({
        title: "Failed to connect Loki",
        description: message,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
      setToken("");
      setPassword("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);

    try {
      const response = await fetch("/api/connected-accounts/loki", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setWebhookUrl(null);
        setInstructions([]);
        setBaseUrl("");
        setAuthType("bearer");
        setToken("");
        setUsername("");
        setPassword("");
        setTenantId("");

        toast({
          title: "Success",
          description: "Loki disconnected successfully",
        });

        if (typeof window !== "undefined") {
          localStorage.removeItem("isLokiConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect");
      }
    } catch (err: unknown) {
      console.error("Loki disconnect failed", err);
      const message = err instanceof Error ? err.message : "Disconnect failed";
      toast({
        title: "Failed to disconnect Loki",
        description: message,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = (text: string) => {
    copyToClipboard(text)
      .then(() => {
        setCopiedField("url");
        toast({ title: "Copied" });
        setTimeout(() => setCopiedField(null), 2000);
      })
      .catch(() => toast({ title: "Failed to copy", variant: "destructive" }));
  };

  // Render webhook loading state
  const renderWebhookContent = () => {
    if (webhookLoading) {
      return (
        <div className="flex items-center justify-center py-12">
          <p className="text-muted-foreground">Loading webhook configuration...</p>
        </div>
      );
    }

    if (webhookError) {
      return (
        <div className="flex flex-col items-center justify-center py-12 gap-4">
          <p className="text-destructive">{webhookError}</p>
          <Button variant="outline" onClick={loadWebhookUrl}>
            Retry
          </Button>
        </div>
      );
    }

    if (webhookUrl && status) {
      return (
        <LokiWebhookStep
          status={status}
          webhookUrl={webhookUrl}
          instructions={instructions}
          copiedField={copiedField}
          onCopyUrl={() => handleCopy(webhookUrl)}
          onDisconnect={handleDisconnect}
          loading={webhookLoading || loading}
        />
      );
    }

    return null;
  };

  // Show loading while checking initial status
  if (isInitialLoading) {
    return (
      <ConnectorAuthGuard connectorName="Loki">
        <div className="container mx-auto py-8 px-4 max-w-3xl">
          <div className="mb-6">
            <h1 className="text-3xl font-bold">Loki Integration</h1>
            <p className="text-muted-foreground mt-1">
              Connect Grafana Loki for log aggregation and alert webhooks
            </p>
          </div>
          <div className="flex items-center justify-center py-12">
            <p className="text-muted-foreground">Loading...</p>
          </div>
        </div>
      </ConnectorAuthGuard>
    );
  }

  return (
    <ConnectorAuthGuard connectorName="Loki">
      <div className="container mx-auto py-8 px-4 max-w-3xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Loki Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect Grafana Loki for log aggregation and alert webhooks
          </p>
        </div>

        {!status?.connected ? (
          <LokiConnectionStep
            baseUrl={baseUrl}
            setBaseUrl={setBaseUrl}
            authType={authType}
            setAuthType={setAuthType}
            token={token}
            setToken={setToken}
            username={username}
            setUsername={setUsername}
            password={password}
            setPassword={setPassword}
            tenantId={tenantId}
            setTenantId={setTenantId}
            loading={loading}
            onConnect={handleConnect}
          />
        ) : (
          renderWebhookContent()
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { netdataService, NetdataStatus } from "@/lib/services/netdata";
import { NetdataConnectionStep } from "@/components/netdata/NetdataConnectionStep";
import { NetdataWebhookStep } from "@/components/netdata/NetdataWebhookStep";
import { Button } from "@/components/ui/button";

export default function NetdataAuthPage() {
  const { toast } = useToast();
  const [apiToken, setApiToken] = useState("");
  const [spaceName, setSpaceName] = useState("");
  const [status, setStatus] = useState<NetdataStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [verificationToken, setVerificationToken] = useState<string | undefined>();
  const [webhookLoading, setWebhookLoading] = useState(false);
  const [webhookError, setWebhookError] = useState<string | null>(null);
  const [copiedField, setCopiedField] = useState<"url" | "token" | null>(null);

  const loadStatus = async () => {
    try {
      const result = await netdataService.getStatus();
      setStatus(result);
      if (result?.connected) {
        await loadWebhookUrl();
      }
    } catch (err) {
      console.error("Failed to load Netdata status", err);
    } finally {
      setIsInitialLoading(false);
    }
  };

  const loadWebhookUrl = async () => {
    setWebhookLoading(true);
    setWebhookError(null);
    try {
      const response = await netdataService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      setVerificationToken(response.verificationToken);
    } catch (err) {
      console.error("Failed to load webhook URL", err);
      const message = err instanceof Error ? err.message : "Failed to load webhook URL";
      setWebhookError(message);
      toast({ title: "Error loading webhook configuration, please try again.", description: message, variant: "destructive" });
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
      const payload = { apiToken, spaceName: spaceName || undefined };
      const result = await netdataService.connect(payload);
      setStatus(result);
      
      toast({
        title: "Success",
        description: "Netdata connected! Now configure the webhook below.",
      });
      
      await loadWebhookUrl();

      if (typeof window !== "undefined") {
        localStorage.setItem("isNetdataConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    } catch (err: unknown) {
      console.error("Netdata connection failed", err);
      const message = err instanceof Error ? err.message : "Connection failed";
      toast({
        title: "Failed to connect Netdata",
        description: message,
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
      const response = await fetch("/api/connected-accounts/netdata", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setWebhookUrl(null);
        setVerificationToken(undefined);
        setSpaceName("");
        
        toast({
          title: "Success",
          description: "Netdata disconnected successfully",
        });

        if (typeof window !== "undefined") {
          localStorage.removeItem("isNetdataConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect");
      }
    } catch (err: unknown) {
      console.error("Netdata disconnect failed", err);
      const message = err instanceof Error ? err.message : "Disconnect failed";
      toast({
        title: "Failed to disconnect Netdata",
        description: message,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = (text: string, field: "url" | "token") => {
    navigator.clipboard.writeText(text).then(() => {
      setCopiedField(field);
      toast({ title: "Copied" });
      setTimeout(() => setCopiedField(null), 2000);
    }).catch(() => toast({ title: "Failed to copy", variant: "destructive" }));
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
        <NetdataWebhookStep
          status={status}
          webhookUrl={webhookUrl}
          verificationToken={verificationToken}
          copiedField={copiedField}
          onCopyUrl={() => handleCopy(webhookUrl, "url")}
          onCopyToken={() => verificationToken && handleCopy(verificationToken, "token")}
          onRefresh={loadWebhookUrl}
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
      <div className="container mx-auto py-8 px-4 max-w-3xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Netdata Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect Netdata Cloud and receive alerts in Aurora
          </p>
        </div>
        <div className="flex items-center justify-center py-12">
          <p className="text-muted-foreground">Loading...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-3xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Netdata Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect Netdata Cloud and receive alerts in Aurora
        </p>
      </div>

      {!status?.connected ? (
        <NetdataConnectionStep
          apiToken={apiToken}
          setApiToken={setApiToken}
          spaceName={spaceName}
          setSpaceName={setSpaceName}
          loading={loading}
          onConnect={handleConnect}
        />
      ) : (
        renderWebhookContent()
      )}
    </div>
  );
}

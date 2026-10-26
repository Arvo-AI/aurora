"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { rabbitmqService, RabbitMQStatus } from "@/lib/services/rabbitmq";
import { RabbitMQConnectionStep } from "@/components/rabbitmq/RabbitMQConnectionStep";
import { RabbitMQConnectedStatus } from "@/components/rabbitmq/RabbitMQConnectedStatus";
import { apiErrorMessage } from "@/lib/services/api-client";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEY = "rabbitmq_connection_status";

export default function RabbitMQAuthPage() {
  const { toast } = useToast();
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [vhost, setVhost] = useState("");
  const [skipTls, setSkipTls] = useState(false);
  const [status, setStatus] = useState<RabbitMQStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === "undefined") return;
    if (connected) {
      localStorage.setItem("isRabbitMQConnected", "true");
    } else {
      localStorage.removeItem("isRabbitMQConnected");
    }
    window.dispatchEvent(new CustomEvent("providerStateChanged"));
    window.dispatchEvent(new Event("rabbitmqStateChanged"));
  };

  const fetchAndUpdateStatus = async () => {
    const result = await rabbitmqService.getStatus();
    setStatus(result);
    if (typeof window !== "undefined" && result) {
      localStorage.setItem(CACHE_KEY, JSON.stringify(result));
    }
    updateLocalStorageConnection(result?.connected ?? false);
  };

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== "undefined") {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          try {
            const parsed = JSON.parse(cached) as RabbitMQStatus;
            setStatus(parsed);
            updateLocalStorageConnection(parsed?.connected ?? false);
            if (isInitialLoad) {
              setIsInitialLoad(false);
              void fetchAndUpdateStatus().catch(() => {
                toast({
                  title: "Error",
                  description: "Unable to load RabbitMQ status",
                  variant: "destructive",
                });
              });
              return;
            }
            return;
          } catch {
            localStorage.removeItem(CACHE_KEY);
          }
        }
      }
      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error("[rabbitmq] Failed to load status", error);
      toast({
        title: "Error",
        description: "Unable to load RabbitMQ status",
        variant: "destructive",
      });
    }
  };

  useEffect(() => {
    void loadStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);
    try {
      const result = await rabbitmqService.connect({
        url,
        username,
        password,
        vhost,
        verify_tls: !skipTls,
      });
      setStatus({
        connected: true,
        url: result.url,
        username: result.username,
        vhost: result.vhost,
        clusterName: result.clusterName,
        version: result.version,
        tags: result.tags,
        verifyTls: result.verifyTls,
      });
      updateLocalStorageConnection(true);
      toast({
        title: "RabbitMQ connected",
        description: result.clusterName
          ? `Cluster ${result.clusterName} is reachable.`
          : "The management API accepted these credentials.",
      });
      // Connect already succeeded. A status refresh failure must not look like a failed connect.
      try {
        await fetchAndUpdateStatus();
      } catch {
        console.warn("[rabbitmq] Status refresh after connect failed");
      }
      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "rabbitmq" }),
        });
        window.dispatchEvent(new CustomEvent("providerPreferenceChanged", {
          detail: { providers: ["rabbitmq"] },
        }));
      } catch (prefErr: unknown) {
        console.warn("[rabbitmq] Failed to update provider preferences", prefErr);
      }
    } catch (error: unknown) {
      console.error("[rabbitmq] Connect failed", error);
      toast({
        title: "Failed to connect to RabbitMQ",
        description: apiErrorMessage(error, "Could not authenticate with the management API."),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
      setPassword("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/rabbitmq", {
        method: "DELETE",
        credentials: "include",
      });
      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect RabbitMQ");
      }
      setStatus({ connected: false });
      if (typeof window !== "undefined") {
        localStorage.removeItem(CACHE_KEY);
      }
      updateLocalStorageConnection(false);
      toast({ title: "RabbitMQ disconnected" });
      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "remove", provider: "rabbitmq" }),
        });
        window.dispatchEvent(new CustomEvent("providerPreferenceChanged", {
          detail: { providers: [] },
        }));
      } catch (prefErr: unknown) {
        console.warn("[rabbitmq] Failed to update provider preferences", prefErr);
      }
    } catch (error: unknown) {
      console.error("[rabbitmq] Disconnect failed", error);
      toast({
        title: "Failed to disconnect RabbitMQ",
        description: apiErrorMessage(error, "Could not disconnect RabbitMQ."),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const isConnected = Boolean(status?.connected);

  return (
    <ConnectorAuthGuard connectorName="RabbitMQ">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">RabbitMQ</h1>
          <p className="text-muted-foreground mt-1">
            Connect a broker so investigations can read queue depth, consumers, and node alarms.
          </p>
        </div>
        {!isConnected && (
          <RabbitMQConnectionStep
            url={url}
            setUrl={setUrl}
            username={username}
            setUsername={setUsername}
            password={password}
            setPassword={setPassword}
            vhost={vhost}
            setVhost={setVhost}
            skipTls={skipTls}
            setSkipTls={setSkipTls}
            loading={loading}
            onConnect={handleConnect}
          />
        )}
        {isConnected && status && (
          <RabbitMQConnectedStatus
            status={status}
            onDisconnect={handleDisconnect}
            loading={loading}
          />
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

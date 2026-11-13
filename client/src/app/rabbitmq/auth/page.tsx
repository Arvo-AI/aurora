"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { rabbitmqAccounts, rabbitmqService, RabbitMQStatus } from "@/lib/services/rabbitmq";
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
  const [name, setName] = useState("");
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
        name: name.trim() || undefined,
      });
      setStatus(result);
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
      }
      updateLocalStorageConnection(true);
      const added = result.connectedAccount;
      const label = added?.displayName || added?.clusterName;
      toast({
        title: result.replaced ? "RabbitMQ broker updated" : "RabbitMQ connected",
        description: label
          ? `${label} is reachable.`
          : "The management API accepted these credentials.",
      });
      setUrl("");
      setUsername("");
      setVhost("");
      setName("");
      // Connect already succeeded. A status refresh failure must not look like a failed connect.
      try {
        await fetchAndUpdateStatus();
      } catch {
        console.warn("[rabbitmq] Status refresh after connect failed");
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

  const handleDisconnect = async (accountId?: string) => {
    setLoading(true);
    try {
      await rabbitmqService.disconnect(accountId);
      toast({ title: accountId ? "RabbitMQ broker removed" : "RabbitMQ disconnected" });
      if (accountId) {
        // Other brokers stay connected. Refresh so the list and cache match the server.
        await fetchAndUpdateStatus();
      } else {
        setStatus({ connected: false, accounts: [] });
        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEY);
        }
        updateLocalStorageConnection(false);
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

  const accounts = rabbitmqAccounts(status);
  const isConnected = accounts.length > 0;
  const formProps = {
    url,
    setUrl,
    username,
    setUsername,
    password,
    setPassword,
    vhost,
    setVhost,
    name,
    setName,
    loading,
    onConnect: handleConnect,
  };

  return (
    <ConnectorAuthGuard connectorName="RabbitMQ">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">RabbitMQ</h1>
          <p className="text-muted-foreground mt-1">
            Connect one or more brokers so investigations can read queue depth, consumers, and node alarms.
          </p>
        </div>
        {!isConnected && <RabbitMQConnectionStep {...formProps} />}
        {isConnected && (
          <div className="space-y-6">
            <RabbitMQConnectedStatus
              accounts={accounts}
              onDisconnect={handleDisconnect}
              loading={loading}
            />
            <RabbitMQConnectionStep {...formProps} addAnother />
          </div>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

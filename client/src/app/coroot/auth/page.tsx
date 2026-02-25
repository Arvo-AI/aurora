"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { corootService, CorootStatus } from "@/lib/services/coroot";
import { CorootConnectionStep } from "@/components/coroot/CorootConnectionStep";
import { CorootConnectedStatus } from "@/components/coroot/CorootConnectedStatus";
import { getUserFriendlyError } from "@/lib/utils";

const CACHE_KEY = "coroot_connection_status";

export default function CorootAuthPage() {
  const { toast } = useToast();
  const [url, setUrl] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<CorootStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === "undefined") return;
    if (connected) {
      localStorage.setItem("isCorootConnected", "true");
    } else {
      localStorage.removeItem("isCorootConnected");
    }
    window.dispatchEvent(new CustomEvent("providerStateChanged"));
  };

  const fetchAndUpdateStatus = async () => {
    const result = await corootService.getStatus();
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
            const parsed = JSON.parse(cached) as CorootStatus;
            setStatus(parsed);
            updateLocalStorageConnection(parsed?.connected ?? false);
            if (isInitialLoad) {
              setIsInitialLoad(false);
              fetchAndUpdateStatus();
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
      console.error("[coroot] Failed to load status", error);
      toast({
        title: "Error",
        description: "Unable to load Coroot status",
        variant: "destructive",
      });
    }
  };

  useEffect(() => {
    loadStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const result = await corootService.connect({ url, email, password });

      if (!result.success) {
        updateLocalStorageConnection(false);
        toast({
          title: "Failed to connect to Coroot",
          description: "Could not authenticate with the provided credentials. Please check the URL, email, and password.",
          variant: "destructive",
        });
        return;
      }

      const newStatus: CorootStatus = {
        connected: true,
        url: result.url,
        projects: result.projects,
        email,
      };
      setStatus(newStatus);
      updateLocalStorageConnection(true);

      toast({
        title: "Success",
        description: `Coroot connected successfully. ${result.projects?.length ?? 0} project(s) discovered.`,
      });

      await fetchAndUpdateStatus();

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "coroot" }),
        });
        window.dispatchEvent(
          new CustomEvent("providerPreferenceChanged", {
            detail: { providers: ["coroot"] },
          })
        );
      } catch (prefErr: unknown) {
        console.warn("[coroot] Failed to update provider preferences", prefErr);
      }
    } catch (error: unknown) {
      console.error("[coroot] Connect failed", error);
      const message = getUserFriendlyError(error);
      toast({
        title: "Failed to connect to Coroot",
        description: message,
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
      const response = await fetch("/api/connected-accounts/coroot", {
        method: "DELETE",
        credentials: "include",
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Coroot");
      }

      setStatus({ connected: false });

      if (typeof window !== "undefined") {
        localStorage.removeItem(CACHE_KEY);
      }

      updateLocalStorageConnection(false);

      toast({
        title: "Success",
        description: "Coroot disconnected successfully.",
      });

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "remove", provider: "coroot" }),
        });
        window.dispatchEvent(
          new CustomEvent("providerPreferenceChanged", {
            detail: { providers: [] },
          })
        );
      } catch (prefErr: unknown) {
        console.warn("[coroot] Failed to update provider preferences", prefErr);
      }
    } catch (error: unknown) {
      console.error("[coroot] Disconnect failed", error);
      const message = getUserFriendlyError(error);
      toast({
        title: "Failed to disconnect Coroot",
        description: message,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const isConnected = Boolean(status?.connected);

  return (
    <div className="container mx-auto py-8 px-4 max-w-5xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Coroot Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect your Coroot instance to access metrics, logs, traces, incidents, and more inside Aurora.
        </p>
      </div>

      {!isConnected ? (
        <CorootConnectionStep
          url={url}
          setUrl={setUrl}
          email={email}
          setEmail={setEmail}
          password={password}
          setPassword={setPassword}
          loading={loading}
          onConnect={handleConnect}
        />
      ) : status ? (
        <CorootConnectedStatus
          status={status}
          onDisconnect={handleDisconnect}
          loading={loading}
        />
      ) : null}
    </div>
  );
}

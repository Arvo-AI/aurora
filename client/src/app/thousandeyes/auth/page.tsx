"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { thousandEyesService, ThousandEyesStatus } from "@/lib/services/thousandeyes";
import { ThousandEyesConnectionStep } from "@/components/thousandeyes/ThousandEyesConnectionStep";
import { ThousandEyesConnectedStatus } from "@/components/thousandeyes/ThousandEyesConnectedStatus";
import { getUserFriendlyError } from "@/lib/utils";

const CACHE_KEY = "thousandeyes_connection_status";

export default function ThousandEyesAuthPage() {
  const { toast } = useToast();
  const [apiToken, setApiToken] = useState("");
  const [accountGroupId, setAccountGroupId] = useState("");
  const [status, setStatus] = useState<ThousandEyesStatus | null>(null);
  const [loading, setLoading] = useState(false);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (connected) {
      localStorage.setItem("isThousandEyesConnected", "true");
    } else {
      localStorage.removeItem("isThousandEyesConnected");
    }
    window.dispatchEvent(new CustomEvent("providerStateChanged"));
  };

  const fetchAndUpdateStatus = async () => {
    const result = await thousandEyesService.getStatus();
    setStatus(result);

    if (result) {
      localStorage.setItem(CACHE_KEY, JSON.stringify(result));
    }

    updateLocalStorageConnection(result?.connected ?? false);
  };

  const loadStatus = async () => {
    try {
      // Show cached status immediately, then refresh in the background
      const cached = localStorage.getItem(CACHE_KEY);
      if (cached) {
        try {
          const parsed = JSON.parse(cached) as ThousandEyesStatus;
          setStatus(parsed);
          updateLocalStorageConnection(parsed?.connected ?? false);
          fetchAndUpdateStatus();
          return;
        } catch {
          localStorage.removeItem(CACHE_KEY);
        }
      }

      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error("[thousandeyes] Failed to load status", error);
      toast({
        title: "Error",
        description: "Unable to load ThousandEyes status",
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
      const result = await thousandEyesService.connect({
        api_token: apiToken,
        account_group_id: accountGroupId || undefined,
      });

      if (!result.success) {
        updateLocalStorageConnection(false);
        toast({
          title: "Failed to connect to ThousandEyes",
          description: "Could not authenticate with the provided token. Please check your Bearer Token.",
          variant: "destructive",
        });
        return;
      }

      const newStatus: ThousandEyesStatus = {
        connected: true,
        account_group_id: accountGroupId || undefined,
        account_groups: result.account_groups,
      };
      setStatus(newStatus);
      updateLocalStorageConnection(true);

      toast({
        title: "Success",
        description: `ThousandEyes connected successfully. ${result.account_groups?.length ?? 0} account group(s) found.`,
      });

      await fetchAndUpdateStatus();

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "thousandeyes" }),
        });
        window.dispatchEvent(
          new CustomEvent("providerPreferenceChanged", {
            detail: { providers: ["thousandeyes"] },
          })
        );
      } catch (prefErr: unknown) {
        console.warn("[thousandeyes] Failed to update provider preferences", prefErr);
      }
    } catch (error: unknown) {
      console.error("[thousandeyes] Connect failed", error);
      const message = getUserFriendlyError(error);
      toast({
        title: "Failed to connect to ThousandEyes",
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
      const response = await fetch("/api/connected-accounts/thousandeyes", {
        method: "DELETE",
        credentials: "include",
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect ThousandEyes");
      }

      setStatus({ connected: false });
      localStorage.removeItem(CACHE_KEY);
      updateLocalStorageConnection(false);

      toast({
        title: "Success",
        description: "ThousandEyes disconnected successfully.",
      });

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "remove", provider: "thousandeyes" }),
        });
        window.dispatchEvent(
          new CustomEvent("providerPreferenceChanged", {
            detail: { providers: [] },
          })
        );
      } catch (prefErr: unknown) {
        console.warn("[thousandeyes] Failed to update provider preferences", prefErr);
      }
    } catch (error: unknown) {
      console.error("[thousandeyes] Disconnect failed", error);
      const message = getUserFriendlyError(error);
      toast({
        title: "Failed to disconnect ThousandEyes",
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
        <h1 className="text-3xl font-bold">ThousandEyes Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect your ThousandEyes account to access network intelligence, test results, alerts, and Internet Insights inside Aurora.
        </p>
      </div>

      {isConnected && status ? (
        <ThousandEyesConnectedStatus
          status={status}
          onDisconnect={handleDisconnect}
          loading={loading}
        />
      ) : (
        <ThousandEyesConnectionStep
          apiToken={apiToken}
          setApiToken={setApiToken}
          accountGroupId={accountGroupId}
          setAccountGroupId={setAccountGroupId}
          loading={loading}
          onConnect={handleConnect}
        />
      )}
    </div>
  );
}

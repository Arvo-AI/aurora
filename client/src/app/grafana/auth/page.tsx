"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { grafanaService, GrafanaStatus } from "@/lib/services/grafana";
import { GrafanaConnectionStep } from "@/components/grafana/GrafanaConnectionStep";
import { GrafanaWebhookStep } from "@/components/grafana/GrafanaWebhookStep";

// Helper function to extract user-friendly error message (removes technical codes)
const getUserFriendlyError = (err: any): string => {
  if (!err) return "An unexpected error occurred. Please try again.";
  
  let errorText = "";
  
  // Extract error from JSON wrapper if present
  if (typeof err.message === "string") {
    try {
      const parsed = JSON.parse(err.message);
      errorText = parsed.error || err.message;
    } catch {
      errorText = err.message;
    }
  } else if (err.error) {
    errorText = typeof err.error === "string" ? err.error : JSON.stringify(err.error);
  } else {
    errorText = err.message || err.toString() || "An unexpected error occurred";
  }
  
  // Remove HTTP status codes (e.g., "401 Client Error:", "503 Server Error:")
  errorText = errorText.replace(/^\d{3}\s+(Client|Server)\s+Error:\s*/i, "");
  
  // Capitalize first letter if it's lowercase
  if (errorText.length > 0) {
    errorText = errorText.charAt(0).toUpperCase() + errorText.slice(1);
  }
  
  return errorText || "An unexpected error occurred. Please try again.";
};

// Cache keys for localStorage
const CACHE_KEYS = {
  STATUS: 'grafana_connection_status',
  WEBHOOK: 'grafana_webhook_url',
};

export default function GrafanaAuthPage() {
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [stackSlug, setStackSlug] = useState("");
  const [status, setStatus] = useState<GrafanaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  // Load from cache first, then optionally refresh in background
  const loadStatus = async (skipCache = false) => {
    try {
      // Check cache first for immediate display
      if (!skipCache && typeof window !== "undefined") {
        const cachedStatus = localStorage.getItem(CACHE_KEYS.STATUS);
        const cachedWebhook = localStorage.getItem(CACHE_KEYS.WEBHOOK);
        
        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus);
          setStatus(parsedStatus);
          if (parsedStatus?.connected) {
            setBaseUrl(parsedStatus.baseUrl ?? "");
            if (cachedWebhook) {
              setWebhookUrl(cachedWebhook);
            }
          }
          
          // On initial load, refresh in background to ensure data is current
          if (isInitialLoad) {
            setIsInitialLoad(false);
            // Fetch silently in background to verify connection is still valid
            fetchAndUpdateStatus();
            return;
          }
          
          // Cache exists and not initial load, just use it
          return;
        }
      }
      
      // No cache or skipCache=true, fetch fresh data
      await fetchAndUpdateStatus();
    } catch (err) {
      console.error("Failed to load Grafana status", err);
      toast({
        title: "Error",
        description: "Unable to load Grafana status",
        variant: "destructive",
      });
    }
  };
  
  // Fetch and update both state and cache
  const fetchAndUpdateStatus = async () => {
    const result = await grafanaService.getStatus();
    setStatus(result);
    
    // Update cache
    if (typeof window !== "undefined") {
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
    }
    
    if (result?.connected) {
      setBaseUrl(result.baseUrl ?? "");
      await loadWebhookUrl();
    } else {
      // Clear webhook cache if disconnected
      if (typeof window !== "undefined") {
        localStorage.removeItem(CACHE_KEYS.WEBHOOK);
      }
    }
  };

  const loadWebhookUrl = async () => {
    try {
      const response = await grafanaService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
      
      // Update cache
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEYS.WEBHOOK, response.webhookUrl);
      }
    } catch (err) {
      console.error("Failed to load webhook URL", err);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = { baseUrl, apiToken, stackSlug: stackSlug || undefined };
      const result = await grafanaService.connect(payload);
      setStatus(result);
      
      // Update cache immediately
      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(result));
      }
      
      toast({
        title: "Success",
        description: "Grafana connected successfully! Now configure the webhook below to receive alerts.",
      });
      loadWebhookUrl();

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "grafana" }),
        });
      } catch (prefErr) {
        console.warn("Failed to update provider preferences", prefErr);
      }

      if (typeof window !== "undefined") {
        localStorage.setItem("isGrafanaConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
        window.dispatchEvent(new CustomEvent("providerPreferenceChanged", { detail: { providers: ["grafana"] } }));
      }
    } catch (err: any) {
      console.error("Grafana connection failed", err);
      const errorMessage = getUserFriendlyError(err);
      toast({
        title: "Failed to connect to Grafana",
        description: errorMessage,
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
      const response = await fetch("/api/connected-accounts/grafana", {
        method: "DELETE",
        credentials: "include",
      });

      // Accept both 200 and 204 as success
      if (response.ok || response.status === 204) {
        // Clear state
        setStatus({ connected: false });
        setWebhookUrl(null);
        setBaseUrl("");
        setStackSlug("");
        
        // Clear cache
        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEYS.STATUS);
          localStorage.removeItem(CACHE_KEYS.WEBHOOK);
        }
        
        toast({
          title: "Success",
          description: "Grafana disconnected successfully",
        });

        // Update localStorage and dispatch events
        if (typeof window !== "undefined") {
          localStorage.removeItem("isGrafanaConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
          window.dispatchEvent(new CustomEvent("providerPreferenceChanged", { detail: { providers: [] } }));
        }

        // Update provider preferences
        try {
          await fetch("/api/provider-preferences", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "remove", provider: "grafana" }),
          });
        } catch (prefErr) {
          console.warn("Failed to update provider preferences", prefErr);
        }
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Grafana");
      }
    } catch (err: any) {
      console.error("Grafana disconnect failed", err);
      const errorMessage = getUserFriendlyError(err);
      toast({
        title: "Failed to disconnect Grafana",
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhook = () => {
    if (webhookUrl) {
      navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      toast({
        title: "Success",
        description: "Webhook URL copied to clipboard",
      });
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-5xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Grafana Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect your Grafana instance and configure alert webhooks
        </p>
      </div>

      {/* Step Progress Indicator */}
      <div className="flex items-center justify-center mb-8">
        <div className="flex items-center">
          <div className={`flex items-center justify-center w-10 h-10 rounded-full ${!status?.connected ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
            1
          </div>
          <div className={`w-24 h-1 ${status?.connected ? 'bg-blue-600' : 'bg-gray-200'}`}></div>
          <div className={`flex items-center justify-center w-10 h-10 rounded-full ${status?.connected ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
            2
          </div>
        </div>
      </div>

      <div className="flex items-center justify-center mb-6 text-sm font-medium">
        <span className={!status?.connected ? 'text-blue-600' : 'text-muted-foreground'}>
          Connect Grafana
        </span>
        <span className="mx-4 text-muted-foreground">→</span>
        <span className={status?.connected ? 'text-blue-600' : 'text-muted-foreground'}>
          Configure Webhook
        </span>
      </div>

      {!status?.connected ? (
        <GrafanaConnectionStep
          baseUrl={baseUrl}
          setBaseUrl={setBaseUrl}
          apiToken={apiToken}
          setApiToken={setApiToken}
          stackSlug={stackSlug}
          setStackSlug={setStackSlug}
          loading={loading}
          onConnect={handleConnect}
        />
      ) : status && webhookUrl ? (
        <GrafanaWebhookStep
          status={status}
          webhookUrl={webhookUrl}
          copied={copied}
          onCopy={handleCopyWebhook}
          onDisconnect={handleDisconnect}
          loading={loading}
        />
      ) : null}
    </div>
  );
}

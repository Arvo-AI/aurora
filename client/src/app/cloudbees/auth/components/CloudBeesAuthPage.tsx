"use client";

import { useEffect, useState, useCallback } from "react";
import { useToast } from "@/hooks/use-toast";
import { Loader2 } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { cloudbeesService } from "@/lib/services/ci-provider";
import type { CIProviderStatus } from "@/lib/services/ci-provider";
import { apiRequest } from "@/lib/services/api-client";
import { ModeSelector } from "./ModeSelector";
import { CredentialForms } from "./CredentialForms";
import { WebhookSetup } from "./WebhookSetup";
import { ConnectedDashboard } from "./ConnectedDashboard";

type ConnectionMode = "oc" | "single" | "pat";

type Step = 1 | 2 | 3 | "connected";

interface DiscoveredController {
  name: string;
  url: string;
  status: string;
}

interface PlatformConnectResponse {
  success: boolean;
  controllers?: DiscoveredController[];
  operations_center?: { username?: string };
}

const CACHE_KEY = "cloudbees_connection_status";
const CONNECTED_KEY = "isCloudBeesConnected";

export default function CloudBeesAuthPage() {
  const { toast } = useToast();

  const [step, setStep] = useState<Step>(1);
  const [mode, setMode] = useState<ConnectionMode>("oc");
  const [loading, setLoading] = useState(false);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [status, setStatus] = useState<CIProviderStatus | null>(null);

  // Single Controller fields
  const [baseUrl, setBaseUrl] = useState("");
  const [username, setUsername] = useState("");
  const [apiToken, setApiToken] = useState("");

  // Operations Center fields
  const [ocUrl, setOcUrl] = useState("");
  const [ocUsername, setOcUsername] = useState("");
  const [ocApiToken, setOcApiToken] = useState("");
  const [rolloutToken, setRolloutToken] = useState("");
  const [controllers, setControllers] = useState<DiscoveredController[]>([]);

  // Personal Access Token fields
  const [platformUrl, setPlatformUrl] = useState("");
  const [pat, setPat] = useState("");

  // Dashboard state (connected view)
  const [summary, setSummary] = useState<any>(null);
  const [webhookInfo, setWebhookInfo] = useState<any>(null);
  const [deployments, setDeployments] = useState<any[]>([]);
  const [rcaEnabled, setRcaEnabled] = useState(true);
  const [rcaLoading, setRcaLoading] = useState(false);

  // Validation
  const [urlError, setUrlError] = useState("");

  const loadStatus = useCallback(async () => {
    setCheckingStatus(true);
    try {
      try {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          const parsed = JSON.parse(cached);
          setStatus(parsed);
        }
      } catch {
        localStorage.removeItem(CACHE_KEY);
      }

      const result = await apiRequest<any>("/api/cloudbees/status?full=true", { method: "GET", cache: "no-store" });
      if (result) {
        setStatus(result);
        const cacheable = { connected: result.connected, baseUrl: result.baseUrl };
        localStorage.setItem(CACHE_KEY, JSON.stringify(cacheable));
        if (result.connected) {
          localStorage.setItem(CONNECTED_KEY, "true");
          setStep("connected");
          if (result.summary) setSummary(result.summary);
          apiRequest<{ controllers?: DiscoveredController[] }>("/api/cloudbees/controllers", { method: "GET", cache: "no-store" }).then(d => {
            if (d?.controllers) setControllers(d.controllers);
          }).catch(() => {});
          apiRequest<any>("/api/cloudbees/webhook-url", { method: "GET", cache: "no-store" }).then(setWebhookInfo).catch(() => {});
          apiRequest<any>("/api/cloudbees/deployments", { method: "GET", cache: "no-store" }).then(d => setDeployments(d?.deployments || [])).catch(() => {});
          apiRequest<any>("/api/cloudbees/rca-settings", { method: "GET", cache: "no-store" }).then(d => setRcaEnabled(d?.rcaEnabled ?? true)).catch(() => {});
        } else {
          localStorage.removeItem(CONNECTED_KEY);
        }
      }
    } catch (err) {
      console.error("Failed to load CloudBees status", err);
    } finally {
      setCheckingStatus(false);
    }
  }, []);

  useEffect(() => { loadStatus(); }, [loadStatus]);

  useEffect(() => {
    if (step === 3 && !webhookInfo) {
      apiRequest<any>("/api/cloudbees/webhook-url", { method: "GET", cache: "no-store" })
        .then(setWebhookInfo).catch(() => {});
    }
  }, [step, webhookInfo]);

  const validateUrl = (url: string): boolean => {
    return url.startsWith("http://") || url.startsWith("https://");
  };

  const handleSingleControllerConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!validateUrl(baseUrl)) {
      setUrlError("URL must start with http:// or https://");
      return;
    }
    setUrlError("");
    setLoading(true);
    try {
      const connectResult = await cloudbeesService.connect({ baseUrl, username, apiToken });
      setStatus(connectResult);
      localStorage.setItem(CONNECTED_KEY, "true");
      globalThis.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await apiRequest("/api/provider-preferences", {
          method: "POST",
          body: JSON.stringify({ action: "add", provider: "cloudbees" }),
        });
      } catch { /* best-effort */ }

      toast({
        title: "CloudBees CI Connected",
        description: `Successfully connected to ${baseUrl}`,
      });
      setStep(3);
    } catch (err: unknown) {
      console.error("CloudBees connection failed", err);
      toast({ title: "Connection Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setApiToken("");
    }
  };

  const handleOCConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!validateUrl(ocUrl)) {
      setUrlError("URL must start with http:// or https://");
      return;
    }
    setUrlError("");
    setLoading(true);
    try {
      const payload: Record<string, string> = {
        oc_url: ocUrl,
        username: ocUsername,
        api_token: ocApiToken,
      };
      if (rolloutToken) {
        payload.fm_api_token = rolloutToken;
      }

      const result = await apiRequest<PlatformConnectResponse>("/api/cloudbees/connect-platform", {
        method: "POST",
        body: JSON.stringify(payload),
        cache: "no-store",
      });

      if (result?.controllers) {
        setControllers(result.controllers);
      }

      const newStatus: CIProviderStatus = {
        connected: true,
        baseUrl: ocUrl,
        username: ocUsername,
      };
      setStatus(newStatus);
      localStorage.setItem(CACHE_KEY, JSON.stringify({ connected: true, baseUrl: ocUrl }));
      localStorage.setItem(CONNECTED_KEY, "true");
      globalThis.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await apiRequest("/api/provider-preferences", {
          method: "POST",
          body: JSON.stringify({ action: "add", provider: "cloudbees" }),
        });
      } catch { /* best-effort */ }

      if (result?.controllers?.length === 0) {
        toast({
          title: "Operations Center Connected",
          description: "Connected successfully, but no managed controllers were found. Check that your account has permission to view controllers in Operations Center.",
          variant: "destructive",
        });
      } else {
        const controllerSuffix = result?.controllers ? ` — ${result.controllers.length} controller(s) discovered` : "";
        toast({
          title: "Operations Center Connected",
          description: `Connected to ${ocUrl}${controllerSuffix}`,
        });
      }
      setStep(3);
    } catch (err: unknown) {
      console.error("CloudBees OC connection failed", err);
      toast({ title: "Connection Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setOcApiToken("");
    }
  };

  const handlePATConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!validateUrl(platformUrl)) {
      setUrlError("URL must start with http:// or https://");
      return;
    }
    setUrlError("");
    setLoading(true);
    try {
      const result = await apiRequest<PlatformConnectResponse>("/api/cloudbees/connect-platform", {
        method: "POST",
        body: JSON.stringify({
          oc_url: platformUrl,
          api_token: pat,
          auth_mode: "pat",
        }),
        cache: "no-store",
      });

      const newStatus: CIProviderStatus = {
        connected: true,
        baseUrl: platformUrl,
        username: result?.operations_center?.username,
      };
      setStatus(newStatus);
      localStorage.setItem(CACHE_KEY, JSON.stringify({ connected: true, baseUrl: platformUrl }));
      localStorage.setItem(CONNECTED_KEY, "true");
      globalThis.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await apiRequest("/api/provider-preferences", {
          method: "POST",
          body: JSON.stringify({ action: "add", provider: "cloudbees" }),
        });
      } catch { /* best-effort */ }

      toast({
        title: "CloudBees Platform Connected",
        description: `Successfully connected to ${platformUrl}`,
      });
      setStep(3);
    } catch (err: unknown) {
      console.error("CloudBees PAT connection failed", err);
      toast({ title: "Connection Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setPat("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      await apiRequest("/api/connected-accounts/cloudbees", { method: "DELETE" });

      try {
        await apiRequest("/api/cloudbees/disconnect-platform", { method: "POST" });
      } catch { /* best-effort */ }

      setStatus({ connected: false });
      setBaseUrl("");
      setUsername("");
      setOcUrl("");
      setOcUsername("");
      setPlatformUrl("");
      setRolloutToken("");
      setControllers([]);
      localStorage.removeItem(CACHE_KEY);
      localStorage.removeItem(CONNECTED_KEY);
      globalThis.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await apiRequest("/api/provider-preferences", {
          method: "POST",
          body: JSON.stringify({ action: "remove", provider: "cloudbees" }),
        });
      } catch { /* best-effort */ }

      toast({ title: "Disconnected", description: "CloudBees CI has been disconnected." });
      setStep(1);
    } catch (err: unknown) {
      console.error("CloudBees disconnect failed", err);
      toast({ title: "Disconnect Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleRcaToggle = async (checked: boolean) => {
    setRcaLoading(true);
    try {
      await apiRequest("/api/cloudbees/rca-settings", {
        method: "PUT",
        body: JSON.stringify({ rcaEnabled: checked }),
      });
      setRcaEnabled(checked);
      toast({ title: checked ? "RCA Enabled" : "RCA Disabled", description: checked ? "Auto-trigger RCA on failures is now active" : "Auto-trigger RCA has been turned off" });
    } catch (err) {
      toast({ title: "Failed to update", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setRcaLoading(false);
    }
  };

  const handleModeSelect = (selectedMode: ConnectionMode) => {
    setMode(selectedMode);
    setStep(2);
    setUrlError("");
  };

  const handleWebhookDone = () => {
    setStep("connected");
    loadStatus();
  };

  const progressStep = step === "connected" ? 3 : step;
  const stepLabel = step === "connected" ? "Complete" : `Step ${step} of 3`;

  if (checkingStatus && !status) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60vh] gap-3">
        <Loader2 className="h-6 w-6 animate-spin text-[#555]" />
        <p className="text-[13px] text-[#555]">Checking connection...</p>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto px-6 py-12">
      {/* Header with logo */}
      <div className="flex items-center gap-3 mb-8">
        <img src="/cloudbees.svg" alt="CloudBees" className="h-8 w-8" />
        <span className="text-[18px] font-semibold text-white">CloudBees</span>
      </div>

      {/* Progress bar */}
      <div className="flex items-center justify-between mb-12">
        <div className="flex items-center gap-0">
          {[1, 2, 3].map((dot, i) => (
            <div key={dot} className="flex items-center">
              <div
                className={`h-2.5 w-2.5 rounded-full transition-colors duration-300 ${
                  progressStep >= dot ? "bg-white" : "bg-white/[0.12]"
                }`}
              />
              {i < 2 && (
                <div
                  className={`w-16 h-[2px] transition-colors duration-300 ${
                    progressStep > dot ? "bg-white" : "bg-white/[0.08]"
                  }`}
                />
              )}
            </div>
          ))}
        </div>
        <span className="text-[13px] text-[#777]">{stepLabel}</span>
      </div>

      {step === 1 && (
        <ModeSelector onSelect={handleModeSelect} />
      )}

      {step === 2 && (
        <CredentialForms
          mode={mode}
          loading={loading}
          urlError={urlError}
          ocUrl={ocUrl}
          setOcUrl={setOcUrl}
          ocUsername={ocUsername}
          setOcUsername={setOcUsername}
          ocApiToken={ocApiToken}
          setOcApiToken={setOcApiToken}
          rolloutToken={rolloutToken}
          setRolloutToken={setRolloutToken}
          baseUrl={baseUrl}
          setBaseUrl={setBaseUrl}
          username={username}
          setUsername={setUsername}
          apiToken={apiToken}
          setApiToken={setApiToken}
          platformUrl={platformUrl}
          setPlatformUrl={setPlatformUrl}
          pat={pat}
          setPat={setPat}
          onOCConnect={handleOCConnect}
          onSingleConnect={handleSingleControllerConnect}
          onPATConnect={handlePATConnect}
          onBack={() => { setStep(1); setUrlError(""); }}
        />
      )}

      {step === 3 && (
        <WebhookSetup
          webhookInfo={webhookInfo}
          onDone={handleWebhookDone}
        />
      )}

      {step === "connected" && (
        <ConnectedDashboard
          status={status}
          summary={summary}
          webhookInfo={webhookInfo}
          deployments={deployments}
          controllers={controllers}
          rcaEnabled={rcaEnabled}
          rcaLoading={rcaLoading}
          loading={loading}
          onDisconnect={handleDisconnect}
          onRcaToggle={handleRcaToggle}
        />
      )}

      <style jsx global>{`
        @keyframes stepIn {
          from {
            opacity: 0;
            transform: translateY(4px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }
        .animate-step-in {
          animation: stepIn 0.3s ease-out forwards;
        }
      `}</style>
    </div>
  );
}

"use client";

import { useEffect, useState, useCallback } from "react";
import { useToast } from "@/hooks/use-toast";
import {
  ChevronRight, Copy, Eye, EyeOff,
  Loader2, Check,
} from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { getUserFriendlyError } from "@/lib/utils";
import { cloudbeesService } from "@/lib/services/ci-provider";
import type { CIProviderStatus } from "@/lib/services/ci-provider";
import { apiRequest } from "@/lib/services/api-client";

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
  const [showToken, setShowToken] = useState(false);

  // Operations Center fields
  const [ocUrl, setOcUrl] = useState("");
  const [ocUsername, setOcUsername] = useState("");
  const [ocApiToken, setOcApiToken] = useState("");
  const [showOcToken, setShowOcToken] = useState(false);
  const [rolloutToken, setRolloutToken] = useState("");
  const [showRolloutToken, setShowRolloutToken] = useState(false);
  const [controllers, setControllers] = useState<DiscoveredController[]>([]);

  // Personal Access Token fields
  const [platformUrl, setPlatformUrl] = useState("");
  const [pat, setPat] = useState("");
  const [showPat, setShowPat] = useState(false);

  // Dashboard state (connected view)
  const [summary, setSummary] = useState<any>(null);
  const [webhookInfo, setWebhookInfo] = useState<any>(null);
  const [deployments, setDeployments] = useState<any[]>([]);
  const [rcaEnabled, setRcaEnabled] = useState(true);
  const [rcaLoading, setRcaLoading] = useState(false);
  const [webhookCopied, setWebhookCopied] = useState(false);

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

      const result = await cloudbeesService.getStatus();
      if (result) {
        setStatus(result);
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        if (result.connected) {
          localStorage.setItem(CONNECTED_KEY, "true");
          setStep("connected");
          try {
            const ctrlResp = await apiRequest<{ controllers?: DiscoveredController[] }>("/api/cloudbees/controllers", {
              method: "GET",
              cache: "no-store",
            });
            if (ctrlResp?.controllers) {
              setControllers(ctrlResp.controllers);
            }
          } catch { /* OC may not be connected — ignore */ }

          // Fetch dashboard data
          fetch("/api/cloudbees/status?full=true").then(r => r.json()).then(d => {
            if (d?.summary) setSummary(d.summary);
          }).catch(() => {});
          fetch("/api/cloudbees/webhook-url").then(r => r.json()).then(setWebhookInfo).catch(() => {});
          fetch("/api/cloudbees/deployments").then(r => r.json()).then(d => setDeployments(d.deployments || [])).catch(() => {});
          fetch("/api/cloudbees/rca-settings").then(r => r.json()).then(d => setRcaEnabled(d.rcaEnabled ?? true)).catch(() => {});
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
      fetch("/api/cloudbees/webhook-url").then(r => {
        if (!r.ok) throw new Error("Failed to fetch webhook URL");
        return r.json();
      }).then(setWebhookInfo).catch(() => {});
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
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
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
      setShowToken(false);
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
      localStorage.setItem(CACHE_KEY, JSON.stringify(newStatus));
      localStorage.setItem(CONNECTED_KEY, "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "cloudbees" }),
        });
      } catch { /* best-effort */ }

      if (result?.controllers && result.controllers.length === 0) {
        toast({
          title: "Operations Center Connected",
          description: "Connected successfully, but no managed controllers were found. Check that your account has permission to view controllers in Operations Center.",
          variant: "destructive",
        });
      } else {
        toast({
          title: "Operations Center Connected",
          description: `Connected to ${ocUrl}${result?.controllers ? ` — ${result.controllers.length} controller(s) discovered` : ""}`,
        });
      }
      setStep(3);
    } catch (err: unknown) {
      console.error("CloudBees OC connection failed", err);
      toast({ title: "Connection Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setOcApiToken("");
      setShowOcToken(false);
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
      localStorage.setItem(CACHE_KEY, JSON.stringify(newStatus));
      localStorage.setItem(CONNECTED_KEY, "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
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
      setShowPat(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/cloudbees", { method: "DELETE", credentials: "include" });
      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect CloudBees");
      }

      try {
        await fetch("/api/cloudbees/disconnect-platform", { method: "POST", credentials: "include" });
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
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
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
      const response = await fetch("/api/cloudbees/rca-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rcaEnabled: checked }),
      });
      if (!response.ok) {
        throw new Error("Failed to update RCA settings");
      }
      setRcaEnabled(checked);
      toast({ title: checked ? "RCA Enabled" : "RCA Disabled", description: checked ? "Auto-trigger RCA on failures is now active" : "Auto-trigger RCA has been turned off" });
    } catch (err) {
      toast({ title: "Failed to update", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setRcaLoading(false);
    }
  };

  const copyDashboardWebhookUrl = () => {
    if (!webhookInfo?.webhookUrl) return;
    navigator.clipboard.writeText(webhookInfo.webhookUrl);
    setWebhookCopied(true);
    toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
    setTimeout(() => setWebhookCopied(false), 2000);
  };

  const timeAgo = (dateStr: string | null | undefined): string => {
    if (!dateStr) return "";
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return "";
    const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "just now";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  };


  // --- Progress bar ---
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

      {/* Step 1: Choose mode */}
      {step === 1 && (
        <div className="animate-step-in">
          <h1 className="text-[28px] font-bold tracking-tight mb-3">How should Aurora connect?</h1>
          <p className="text-[15px] text-[#777] mb-10">
            Choose based on your CloudBees setup. You can always change this later.
          </p>

          <div className="space-y-3">
            <button
              type="button"
              onClick={() => { setMode("oc"); setStep(2); setUrlError(""); }}
              className="group w-full p-6 rounded-2xl border border-white/[0.06] hover:border-white/[0.12] bg-white/[0.01] hover:bg-white/[0.02] transition-all text-left flex items-center justify-between"
            >
              <div>
                <p className="text-[15px] font-medium mb-1">Operations Center</p>
                <p className="text-[13px] text-[#777] leading-relaxed">
                  Recommended for teams with multiple Jenkins controllers. Aurora discovers all controllers and can investigate across them.
                </p>
              </div>
              <ChevronRight className="h-4 w-4 text-[#555] flex-shrink-0 ml-4 group-hover:translate-x-1 transition-transform" />
            </button>

            <button
              type="button"
              onClick={() => { setMode("single"); setStep(2); setUrlError(""); }}
              className="group w-full p-6 rounded-2xl border border-white/[0.06] hover:border-white/[0.12] bg-white/[0.01] hover:bg-white/[0.02] transition-all text-left flex items-center justify-between"
            >
              <div>
                <p className="text-[15px] font-medium mb-1">Single Controller</p>
                <p className="text-[13px] text-[#777] leading-relaxed">
                  Connect directly to one CloudBees CI or Jenkins instance.
                </p>
              </div>
              <ChevronRight className="h-4 w-4 text-[#555] flex-shrink-0 ml-4 group-hover:translate-x-1 transition-transform" />
            </button>

            <button
              type="button"
              onClick={() => { setMode("pat"); setStep(2); setUrlError(""); }}
              className="group w-full p-6 rounded-2xl border border-white/[0.06] hover:border-white/[0.12] bg-white/[0.01] hover:bg-white/[0.02] transition-all text-left flex items-center justify-between"
            >
              <div>
                <p className="text-[15px] font-medium mb-1">Personal Access Token</p>
                <p className="text-[13px] text-[#777] leading-relaxed">
                  Use a platform-level PAT for authentication.
                </p>
              </div>
              <ChevronRight className="h-4 w-4 text-[#555] flex-shrink-0 ml-4 group-hover:translate-x-1 transition-transform" />
            </button>
          </div>

          <p className="text-[13px] text-[#555] mt-8">
            Not sure? Most teams with CloudBees CI use Operations Center.
          </p>
        </div>
      )}

      {/* Step 2: Credentials */}
      {step === 2 && (
        <div className="animate-step-in">
          {mode === "oc" && (
            <>
              <h1 className="text-[28px] font-bold tracking-tight mb-3">Connect your Operations Center</h1>
              <p className="text-[15px] text-[#777] mb-10">
                Aurora will discover your managed controllers and monitor deployments across all of them.
              </p>

              {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

              <form onSubmit={handleOCConnect} className="space-y-6">
                <div>
                  <label htmlFor="oc-url" className="block text-[13px] text-[#999] mb-2">Operations Center URL</label>
                  <input
                    id="oc-url"
                    type="text"
                    value={ocUrl}
                    onChange={(e) => setOcUrl(e.target.value)}
                    placeholder="https://cjoc.company.com"
                    required
                    disabled={loading}
                    className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                  />
                </div>
                <div>
                  <label htmlFor="oc-username" className="block text-[13px] text-[#999] mb-2">Username</label>
                  <input
                    id="oc-username"
                    type="text"
                    value={ocUsername}
                    onChange={(e) => setOcUsername(e.target.value)}
                    placeholder="your-cloudbees-username"
                    required
                    disabled={loading}
                    className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                  />
                </div>
                <div>
                  <label htmlFor="oc-token" className="block text-[13px] text-[#999] mb-2">API Token</label>
                  <div className="relative">
                    <input
                      id="oc-token"
                      type={showOcToken ? "text" : "password"}
                      value={ocApiToken}
                      onChange={(e) => setOcApiToken(e.target.value)}
                      placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                      required
                      disabled={loading}
                      className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                    />
                    <button
                      type="button"
                      onClick={() => setShowOcToken(!showOcToken)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors"
                      aria-label={showOcToken ? "Hide token" : "Show token"}
                    >
                      {showOcToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                </div>

                {/* Inline instruction box */}
                <div className="rounded-xl bg-white/[0.02] border border-white/[0.04] p-5">
                  <p className="text-[13px] text-[#999] mb-3">Where to find your token:</p>
                  <ol className="text-[13px] text-[#777] space-y-1.5 list-decimal list-inside">
                    <li>Log in to Operations Center</li>
                    <li>Click your username (top-right)</li>
                    <li>Go to Configure &rarr; API Token</li>
                    <li>Click "Add new Token" and copy it</li>
                  </ol>
                </div>

                {/* Feature Management (optional) */}
                <details>
                  <summary className="text-[13px] text-[#777] cursor-pointer hover:text-[#999] transition-colors">
                    Feature Management (optional)
                  </summary>
                  <div className="mt-4">
                    <label htmlFor="rollout-token" className="block text-[13px] text-[#999] mb-2">Feature Management API Token</label>
                    <div className="relative">
                      <input
                        id="rollout-token"
                        type={showRolloutToken ? "text" : "password"}
                        value={rolloutToken}
                        onChange={(e) => setRolloutToken(e.target.value)}
                        placeholder="Bearer token from Feature Management"
                        disabled={loading}
                        className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                      />
                      <button
                        type="button"
                        onClick={() => setShowRolloutToken(!showRolloutToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors"
                        aria-label={showRolloutToken ? "Hide token" : "Show token"}
                      >
                        {showRolloutToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    </div>
                    <p className="text-[11px] text-[#444] mt-2">Enables feature flag change correlation during incident investigation</p>
                  </div>
                </details>

                <div className="flex items-center gap-3 pt-4">
                  <button
                    type="button"
                    onClick={() => { setStep(1); setUrlError(""); }}
                    className="text-[15px] text-[#777] hover:text-white transition-colors px-4 py-3"
                  >
                    Back
                  </button>
                  <button
                    type="submit"
                    disabled={loading || !ocUrl || !ocUsername || !ocApiToken}
                    className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                    {loading ? "Connecting..." : "Connect"}
                  </button>
                </div>
              </form>
            </>
          )}

          {mode === "single" && (
            <>
              <h1 className="text-[28px] font-bold tracking-tight mb-3">Connect your controller</h1>
              <p className="text-[15px] text-[#777] mb-10">
                Enter your CloudBees CI instance details.
              </p>

              {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

              <form onSubmit={handleSingleControllerConnect} className="space-y-6">
                <div>
                  <label htmlFor="sc-url" className="block text-[13px] text-[#999] mb-2">Controller URL</label>
                  <input
                    id="sc-url"
                    type="text"
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="https://cloudbees.example.com"
                    required
                    disabled={loading}
                    className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                  />
                </div>
                <div>
                  <label htmlFor="sc-username" className="block text-[13px] text-[#999] mb-2">Username</label>
                  <input
                    id="sc-username"
                    type="text"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="your-cloudbees-username"
                    required
                    disabled={loading}
                    className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                  />
                </div>
                <div>
                  <label htmlFor="sc-token" className="block text-[13px] text-[#999] mb-2">API Token</label>
                  <div className="relative">
                    <input
                      id="sc-token"
                      type={showToken ? "text" : "password"}
                      value={apiToken}
                      onChange={(e) => setApiToken(e.target.value)}
                      placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                      required
                      disabled={loading}
                      className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                    />
                    <button
                      type="button"
                      onClick={() => setShowToken(!showToken)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors"
                      aria-label={showToken ? "Hide token" : "Show token"}
                    >
                      {showToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                  <p className="text-[11px] text-[#444] mt-2">Your profile &rarr; Security &rarr; API Token &rarr; Generate</p>
                </div>

                <div className="flex items-center gap-3 pt-4">
                  <button
                    type="button"
                    onClick={() => { setStep(1); setUrlError(""); }}
                    className="text-[15px] text-[#777] hover:text-white transition-colors px-4 py-3"
                  >
                    Back
                  </button>
                  <button
                    type="submit"
                    disabled={loading || !baseUrl || !username || !apiToken}
                    className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                    {loading ? "Connecting..." : "Connect"}
                  </button>
                </div>
              </form>
            </>
          )}

          {mode === "pat" && (
            <>
              <h1 className="text-[28px] font-bold tracking-tight mb-3">Platform authentication</h1>
              <p className="text-[15px] text-[#777] mb-10">
                Enter your CloudBees platform URL and access token.
              </p>

              {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

              <form onSubmit={handlePATConnect} className="space-y-6">
                <div>
                  <label htmlFor="pat-url" className="block text-[13px] text-[#999] mb-2">Platform URL</label>
                  <input
                    id="pat-url"
                    type="text"
                    value={platformUrl}
                    onChange={(e) => setPlatformUrl(e.target.value)}
                    placeholder="https://your-org.cloudbees.io"
                    required
                    disabled={loading}
                    className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                  />
                </div>
                <div>
                  <label htmlFor="pat-token" className="block text-[13px] text-[#999] mb-2">Personal Access Token</label>
                  <div className="relative">
                    <input
                      id="pat-token"
                      type={showPat ? "text" : "password"}
                      value={pat}
                      onChange={(e) => setPat(e.target.value)}
                      placeholder="cbp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                      required
                      disabled={loading}
                      className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPat(!showPat)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors"
                      aria-label={showPat ? "Hide token" : "Show token"}
                    >
                      {showPat ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                  <p className="text-[11px] text-[#444] mt-2">Profile &rarr; Personal access tokens &rarr; Create</p>
                </div>

                <div className="flex items-center gap-3 pt-4">
                  <button
                    type="button"
                    onClick={() => { setStep(1); setUrlError(""); }}
                    className="text-[15px] text-[#777] hover:text-white transition-colors px-4 py-3"
                  >
                    Back
                  </button>
                  <button
                    type="submit"
                    disabled={loading || !platformUrl || !pat}
                    className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                    {loading ? "Connecting..." : "Connect"}
                  </button>
                </div>
              </form>
            </>
          )}
        </div>
      )}

      {/* Step 3: Webhook setup */}
      {step === 3 && (
        <div className="animate-step-in">
          <h1 className="text-[28px] font-bold tracking-tight mb-3">Set up deployment tracking</h1>
          <p className="text-[15px] text-[#777] mb-10">
            Add this webhook to your Jenkinsfile so Aurora is notified when deployments complete.
          </p>

          {webhookInfo ? (
            <div className="space-y-6">
              <div>
                <p className="block text-[13px] text-[#999] mb-2">Webhook URL</p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 text-[13px] text-[#777] bg-white/[0.02] border border-white/[0.06] px-4 py-3.5 rounded-xl truncate">
                    {webhookInfo.webhookUrl}
                  </code>
                  <button
                    type="button"
                    onClick={copyDashboardWebhookUrl}
                    className="p-3.5 rounded-xl border border-white/[0.06] hover:bg-white/[0.04] transition-colors"
                  >
                    {webhookCopied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4 text-[#777]" />}
                  </button>
                </div>
              </div>

              {webhookInfo.jenkinsfileBasic && (
                <div>
                  <p className="block text-[13px] text-[#999] mb-2">Jenkinsfile snippet</p>
                  <div className="relative">
                    <button
                      onClick={() => { navigator.clipboard.writeText(webhookInfo.jenkinsfileBasic); toast({ title: "Copied", description: "Jenkinsfile snippet copied to clipboard" }); }}
                      className="absolute top-3 right-3 p-1.5 rounded-lg bg-white/[0.05] hover:bg-white/[0.1] text-[#666] hover:text-white transition-all"
                      title="Copy snippet"
                    >
                      <Copy className="w-3.5 h-3.5" />
                    </button>
                    <pre className="text-[13px] text-[#999] bg-white/[0.02] border border-white/[0.06] p-5 rounded-xl overflow-x-auto whitespace-pre leading-relaxed">
                      {webhookInfo.jenkinsfileBasic}
                    </pre>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-5 w-5 animate-spin text-[#555]" />
            </div>
          )}

          <div className="flex items-center gap-3 mt-10">
            <button
              type="button"
              onClick={() => setStep("connected")}
              className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors flex items-center justify-center"
            >
              Done
            </button>
          </div>
          <div className="text-center mt-4">
            <button
              type="button"
              onClick={() => setStep("connected")}
              className="text-[13px] text-[#777] hover:text-white transition-colors"
            >
              Skip for now
            </button>
          </div>
        </div>
      )}

      {/* Connected state */}
      {step === "connected" && (
        <div className="animate-step-in">
          <div className="flex items-baseline gap-3 mb-1">
            <h1 className="text-[28px] font-bold tracking-tight">Connected</h1>
            <button
              onClick={handleDisconnect}
              disabled={loading}
              className="text-[13px] text-[#666] hover:text-white transition-colors ml-auto"
            >
              {loading ? "Disconnecting..." : "Disconnect"}
            </button>
          </div>

          <div className="mt-6 space-y-4">
            <div>
              <p className="text-[13px] text-[#999] mb-1">URL</p>
              <p className="text-[15px]">{status?.baseUrl}</p>
            </div>
            {status?.username && (
              <div>
                <p className="text-[13px] text-[#999] mb-1">User</p>
                <p className="text-[15px]">{status.username}</p>
              </div>
            )}
            {status?.server?.version && status.server.version !== "unknown" && (
              <div>
                <p className="text-[13px] text-[#999] mb-1">Version</p>
                <p className="text-[15px]">{status.server.version}</p>
              </div>
            )}
          </div>

          {/* Stats grid */}
          {summary && (
            <div className="grid grid-cols-4 gap-4 mt-8 mb-8">
              <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
                <p className="text-[24px] font-bold">{summary.jobCount ?? "—"}</p>
                <p className="text-[11px] text-[#666] mt-1">Jobs</p>
              </div>
              <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
                <p className="text-[24px] font-bold">{summary.nodesOnline ?? "—"}<span className="text-[14px] text-[#555] font-normal">/{(summary.nodesOnline ?? 0) + (summary.nodesOffline ?? 0)}</span></p>
                <p className="text-[11px] text-[#666] mt-1">Nodes online</p>
              </div>
              <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
                <p className="text-[24px] font-bold">{summary.busyExecutors ?? "—"}<span className="text-[14px] text-[#555] font-normal">/{summary.totalExecutors ?? 0}</span></p>
                <p className="text-[11px] text-[#666] mt-1">Executors busy</p>
              </div>
              <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
                <p className="text-[24px] font-bold">{summary.queueSize ?? "—"}</p>
                <p className="text-[11px] text-[#666] mt-1">Queue</p>
              </div>
            </div>
          )}

          {/* Job health bar */}
          {summary?.jobHealth && (() => {
            const health = summary.jobHealth;
            const total = (health.healthy || 0) + (health.unstable || 0) + (health.failing || 0) + (health.disabled || 0) + (health.other || 0);
            if (total === 0) return null;
            return (
              <div className="mb-8">
                <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-3">Job Health</p>
                <div className="h-2 rounded-full overflow-hidden bg-white/[0.04] flex">
                  {health.healthy > 0 && <div className="bg-emerald-400" style={{ width: `${(health.healthy / total) * 100}%` }} />}
                  {health.unstable > 0 && <div className="bg-yellow-400" style={{ width: `${(health.unstable / total) * 100}%` }} />}
                  {health.failing > 0 && <div className="bg-red-400" style={{ width: `${(health.failing / total) * 100}%` }} />}
                  {health.disabled > 0 && <div className="bg-[#555]" style={{ width: `${(health.disabled / total) * 100}%` }} />}
                  {health.other > 0 && <div className="bg-[#444]" style={{ width: `${(health.other / total) * 100}%` }} />}
                </div>
                <div className="flex gap-4 mt-2 text-[11px] text-[#666]">
                  {health.healthy > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-emerald-400" />{health.healthy} healthy</span>}
                  {health.unstable > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-yellow-400" />{health.unstable} unstable</span>}
                  {health.failing > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-400" />{health.failing} failing</span>}
                  {health.disabled > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-[#555]" />{health.disabled} disabled</span>}
                </div>
              </div>
            );
          })()}

          {/* RCA toggle */}
          <div className="flex items-center justify-between py-4 border-t border-white/[0.04]">
            <div>
              <p className="text-[14px] text-white">Auto-trigger RCA on failures</p>
              <p className="text-[12px] text-[#666]">Automatically investigate when a build fails</p>
            </div>
            <Switch checked={rcaEnabled} onCheckedChange={handleRcaToggle} disabled={rcaLoading} />
          </div>

          {/* Webhook section */}
          {webhookInfo && (
            <div className="mt-8 pt-6 border-t border-white/[0.04]">
              <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-4">Deployment Webhook</p>
              <div className="flex gap-2 mb-4">
                <code className="flex-1 px-4 py-3 rounded-xl bg-white/[0.02] border border-white/[0.04] text-[12px] text-[#888] font-mono truncate">{webhookInfo.webhookUrl}</code>
                <button
                  type="button"
                  onClick={copyDashboardWebhookUrl}
                  className="px-3 py-3 rounded-xl border border-white/[0.06] hover:bg-white/[0.04] transition-colors"
                >
                  {webhookCopied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4 text-[#777]" />}
                </button>
              </div>
              {webhookInfo.jenkinsfileBasic && (
                <details>
                  <summary className="text-[12px] text-[#666] cursor-pointer hover:text-[#999] transition-colors">View Jenkinsfile snippet</summary>
                  <div className="relative mt-3">
                    <button
                      onClick={() => { navigator.clipboard.writeText(webhookInfo.jenkinsfileBasic); toast({ title: "Copied", description: "Jenkinsfile snippet copied to clipboard" }); }}
                      className="absolute top-3 right-3 p-1.5 rounded-lg bg-white/[0.05] hover:bg-white/[0.1] text-[#666] hover:text-white transition-all"
                      title="Copy snippet"
                    >
                      <Copy className="w-3.5 h-3.5" />
                    </button>
                    <pre className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04] text-[11px] text-[#888] font-mono overflow-x-auto whitespace-pre">{webhookInfo.jenkinsfileBasic}</pre>
                  </div>
                </details>
              )}
            </div>
          )}

          {/* Recent deployments */}
          {deployments.length > 0 && (
            <div className="mt-8 pt-6 border-t border-white/[0.04]">
              <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-4">Recent Deployments</p>
              <div className="space-y-2">
                {deployments.slice(0, 5).map((d, i) => (
                  <div key={i} className="flex items-center justify-between py-2.5 px-4 rounded-xl bg-white/[0.02]">
                    <div className="flex items-center gap-3">
                      <div className={`w-2 h-2 rounded-full ${d.result === "SUCCESS" ? "bg-emerald-400" : d.result === "FAILURE" ? "bg-red-400" : "bg-[#666]"}`} />
                      <span className="text-[13px]">{d.service}</span>
                      {d.environment && <span className="text-[11px] text-[#555]">{d.environment}</span>}
                    </div>
                    <span className="text-[11px] text-[#555]">#{d.buildNumber} · {timeAgo(d.receivedAt)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Managed controllers (OC mode) */}
          {controllers.length > 0 && (
            <div className="mt-8 pt-6 border-t border-white/[0.04]">
              <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-4">Managed Controllers</p>
              <div className="space-y-1">
                {controllers.map((ctrl) => (
                  <div key={ctrl.url} className="flex items-center justify-between py-3 px-4 rounded-xl bg-white/[0.02]">
                    <div className="flex-1 min-w-0">
                      <p className="text-[14px] truncate">{ctrl.name}</p>
                      <p className="text-[12px] text-[#555] truncate">{ctrl.url}</p>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`h-2 w-2 rounded-full ${ctrl.status === "online" ? "bg-green-500" : "bg-[#555]"}`} />
                      <span className="text-[12px] text-[#777]">{ctrl.status}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <style jsx>{`
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

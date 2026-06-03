"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  ChevronLeft, ChevronRight, Copy, Eye, EyeOff,
  Loader2,
} from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { cloudbeesService } from "@/lib/services/ci-provider";
import type { CIProviderStatus } from "@/lib/services/ci-provider";
import { apiRequest } from "@/lib/services/api-client";

type ConnectionMode = "single-controller" | "operations-center" | "personal-access-token";

type View = "picker" | "form";

interface DiscoveredController {
  name: string;
  url: string;
  status: string;
}

interface PlatformConnectResponse {
  success: boolean;
  controllers?: DiscoveredController[];
  baseUrl?: string;
  username?: string;
}

const CACHE_KEY = "cloudbees_connection_status";
const CONNECTED_KEY = "isCloudBeesConnected";

export default function CloudBeesAuthPage() {
  const router = useRouter();
  const { toast } = useToast();

  const [view, setView] = useState<View>("picker");
  const [mode, setMode] = useState<ConnectionMode>("single-controller");
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

  // Validation
  const [urlError, setUrlError] = useState("");

  const loadStatus = async () => {
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
          // Fetch controllers from OC if platform is connected
          try {
            const ctrlResp = await apiRequest<{ controllers?: DiscoveredController[] }>("/api/cloudbees/controllers", {
              method: "GET",
              cache: "no-store",
            });
            if (ctrlResp?.controllers) {
              setControllers(ctrlResp.controllers);
            }
          } catch { /* OC may not be connected — ignore */ }
        } else {
          localStorage.removeItem(CONNECTED_KEY);
        }
      }
    } catch (err) {
      console.error("Failed to load CloudBees status", err);
    } finally {
      setCheckingStatus(false);
    }
  };

  useEffect(() => { loadStatus(); }, []);

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
    } catch (err: unknown) {
      console.error("CloudBees connection failed", err);
      toast({ title: "Connection Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setApiToken("");
      setShowToken(false);
    }
  };

  const validateUrl = (url: string): boolean => {
    return url.startsWith("http://") || url.startsWith("https://");
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
        username: result?.username,
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

      // Also disconnect platform credentials (OC + FM)
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
    } catch (err: unknown) {
      console.error("CloudBees disconnect failed", err);
      toast({ title: "Disconnect Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const selectMode = (m: ConnectionMode) => {
    setMode(m);
    setView("form");
    setUrlError("");
  };

  const isConnected = Boolean(status?.connected);

  if (checkingStatus && !status) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60vh] gap-3">
        <Loader2 className="h-6 w-6 animate-spin text-[#555]" />
        <p className="text-[13px] text-[#555]">Checking connection...</p>
      </div>
    );
  }

  if (isConnected) {
    return (
      <div className="max-w-md mx-auto px-4 py-16">
        <p className="text-[11px] uppercase tracking-[0.15em] text-[#555] mb-8">Connect Integration</p>

        <div className="slide-in">
          <div className="flex items-baseline gap-3 mb-1">
            <h1 className="text-[32px] font-bold tracking-tight">CloudBees</h1>
            <span className="text-[15px] text-green-500">Connected</span>
            <button
              onClick={handleDisconnect}
              disabled={loading}
              className="text-[13px] text-[#666] hover:text-white transition-colors ml-auto"
            >
              {loading ? "Disconnecting..." : "Disconnect"}
            </button>
          </div>

          <div className="mt-10 space-y-4">
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
            {status?.server?.version && (
              <div>
                <p className="text-[13px] text-[#999] mb-1">Version</p>
                <p className="text-[15px]">v{status.server.version}</p>
              </div>
            )}
          </div>

          {controllers.length > 0 && (
            <div className="mt-10">
              <p className="text-[11px] uppercase tracking-[0.15em] text-[#555] mb-4">Controllers</p>
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

          {status?.baseUrl && (
            <div className="mt-10">
              <p className="text-[11px] uppercase tracking-[0.15em] text-[#555] mb-4">Deployment Webhook</p>
              <div className="flex items-center gap-2">
                <code className="flex-1 text-[13px] text-[#777] bg-white/[0.02] px-4 py-3 rounded-xl truncate">
                  {`${window.location.origin}/api/webhooks/cloudbees`}
                </code>
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(`${window.location.origin}/api/webhooks/cloudbees`);
                    toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
                  }}
                  className="p-3 rounded-xl hover:bg-white/[0.04] transition-colors"
                >
                  <Copy className="h-4 w-4 text-[#777]" />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  // Disconnected — show picker or form
  return (
    <div className="max-w-md mx-auto px-4 py-16">
      <p className="text-[11px] uppercase tracking-[0.15em] text-[#555] mb-8">Connect Integration</p>

      {view === "picker" ? (
        <div className="slide-in">
          <h1 className="text-[32px] font-bold tracking-tight mb-3">CloudBees</h1>
          <p className="text-[15px] text-[#777] mb-10">
            Connect your CloudBees CI environment to enable build monitoring, deployment correlation, and incident investigation.
          </p>

          <div className="divide-y divide-white/[0.04]">
            <button
              type="button"
              onClick={() => selectMode("operations-center")}
              className="group w-full flex items-center justify-between py-4 text-left"
            >
              <div>
                <p className="text-[15px] font-medium">Operations Center</p>
                <p className="text-[13px] text-[#777] mt-0.5">All your controllers, one connection</p>
              </div>
              <ChevronRight className="h-4 w-4 text-[#555] group-hover:translate-x-1 transition-transform" />
            </button>
            <button
              type="button"
              onClick={() => selectMode("single-controller")}
              className="group w-full flex items-center justify-between py-4 text-left"
            >
              <div>
                <p className="text-[15px] font-medium">Single controller</p>
                <p className="text-[13px] text-[#777] mt-0.5">Direct connection to one instance</p>
              </div>
              <ChevronRight className="h-4 w-4 text-[#555] group-hover:translate-x-1 transition-transform" />
            </button>
            <button
              type="button"
              onClick={() => selectMode("personal-access-token")}
              className="group w-full flex items-center justify-between py-4 text-left"
            >
              <div>
                <p className="text-[15px] font-medium">Access token</p>
                <p className="text-[13px] text-[#777] mt-0.5">Platform-level PAT</p>
              </div>
              <ChevronRight className="h-4 w-4 text-[#555] group-hover:translate-x-1 transition-transform" />
            </button>
          </div>

          <p className="text-[11px] text-[#444] mt-8">
            Operations Center is recommended for most teams — it discovers all managed controllers automatically.
          </p>
        </div>
      ) : (
        <div className="slide-in">
          <button
            type="button"
            onClick={() => { setView("picker"); setUrlError(""); }}
            className="flex items-center gap-1 text-[13px] text-[#777] hover:text-white transition-colors mb-8"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            Back
          </button>

          {mode === "operations-center" && (
            <OperationsCenterForm
              ocUrl={ocUrl} setOcUrl={setOcUrl}
              ocUsername={ocUsername} setOcUsername={setOcUsername}
              ocApiToken={ocApiToken} setOcApiToken={setOcApiToken}
              showOcToken={showOcToken} setShowOcToken={setShowOcToken}
              rolloutToken={rolloutToken} setRolloutToken={setRolloutToken}
              showRolloutToken={showRolloutToken} setShowRolloutToken={setShowRolloutToken}
              loading={loading} onSubmit={handleOCConnect}
              urlError={urlError}
            />
          )}

          {mode === "single-controller" && (
            <SingleControllerForm
              baseUrl={baseUrl} setBaseUrl={setBaseUrl}
              username={username} setUsername={setUsername}
              apiToken={apiToken} setApiToken={setApiToken}
              showToken={showToken} setShowToken={setShowToken}
              loading={loading} onSubmit={handleSingleControllerConnect}
              urlError={urlError}
            />
          )}

          {mode === "personal-access-token" && (
            <PATForm
              platformUrl={platformUrl} setPlatformUrl={setPlatformUrl}
              pat={pat} setPat={setPat}
              showPat={showPat} setShowPat={setShowPat}
              loading={loading} onSubmit={handlePATConnect}
              urlError={urlError}
            />
          )}
        </div>
      )}
    </div>
  );
}

function SingleControllerForm({
  baseUrl, setBaseUrl, username, setUsername, apiToken, setApiToken,
  showToken, setShowToken, loading, onSubmit, urlError,
}: {
  baseUrl: string; setBaseUrl: (v: string) => void;
  username: string; setUsername: (v: string) => void;
  apiToken: string; setApiToken: (v: string) => void;
  showToken: boolean; setShowToken: (v: boolean) => void;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  urlError: string;
}) {
  return (
    <div>
      <h2 className="text-[24px] font-bold tracking-tight mb-2">Single controller</h2>
      <p className="text-[15px] text-[#777] mb-10">Connect directly to your CloudBees CI instance.</p>

      {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

      <form onSubmit={onSubmit} className="space-y-6">
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
            className="w-full h-11 px-4 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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
            className="w-full h-11 px-4 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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
              className="w-full h-11 px-4 pr-11 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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

        <div className="pt-4">
          <button
            type="submit"
            disabled={loading || !baseUrl || !username || !apiToken}
            className="w-full h-11 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {loading && <Loader2 className="h-4 w-4 animate-spin" />}
            {loading ? "Connecting..." : "Connect"}
          </button>
        </div>
      </form>
    </div>
  );
}

function OperationsCenterForm({
  ocUrl, setOcUrl, ocUsername, setOcUsername, ocApiToken, setOcApiToken,
  showOcToken, setShowOcToken, rolloutToken, setRolloutToken,
  showRolloutToken, setShowRolloutToken,
  loading, onSubmit, urlError,
}: {
  ocUrl: string; setOcUrl: (v: string) => void;
  ocUsername: string; setOcUsername: (v: string) => void;
  ocApiToken: string; setOcApiToken: (v: string) => void;
  showOcToken: boolean; setShowOcToken: (v: boolean) => void;
  rolloutToken: string; setRolloutToken: (v: string) => void;
  showRolloutToken: boolean; setShowRolloutToken: (v: boolean) => void;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  urlError: string;
}) {
  return (
    <div>
      <h2 className="text-[24px] font-bold tracking-tight mb-2">Operations Center</h2>
      <p className="text-[15px] text-[#777] mb-10">We'll discover your managed controllers automatically.</p>

      {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

      <form onSubmit={onSubmit} className="space-y-6">
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
            className="w-full h-11 px-4 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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
            className="w-full h-11 px-4 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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
              className="w-full h-11 px-4 pr-11 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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
          <p className="text-[11px] text-[#444] mt-2">Your profile &rarr; Security &rarr; API Token &rarr; Generate</p>
        </div>

        <details className="mt-10">
          <summary className="text-[13px] text-[#777] cursor-pointer hover:text-[#999] transition-colors">
            Feature flag correlation (optional)
          </summary>
          <div className="mt-4">
            <label htmlFor="rollout-token" className="block text-[13px] text-[#999] mb-2">Rollout API Token</label>
            <div className="relative">
              <input
                id="rollout-token"
                type={showRolloutToken ? "text" : "password"}
                value={rolloutToken}
                onChange={(e) => setRolloutToken(e.target.value)}
                placeholder="Bearer token from Feature Management"
                disabled={loading}
                className="w-full h-11 px-4 pr-11 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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

        <div className="pt-4">
          <button
            type="submit"
            disabled={loading || !ocUrl || !ocUsername || !ocApiToken}
            className="w-full h-11 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {loading && <Loader2 className="h-4 w-4 animate-spin" />}
            {loading ? "Connecting..." : "Connect"}
          </button>
        </div>
      </form>
    </div>
  );
}

function PATForm({
  platformUrl, setPlatformUrl, pat, setPat, showPat, setShowPat, loading, onSubmit, urlError,
}: {
  platformUrl: string; setPlatformUrl: (v: string) => void;
  pat: string; setPat: (v: string) => void;
  showPat: boolean; setShowPat: (v: boolean) => void;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  urlError: string;
}) {
  return (
    <div>
      <h2 className="text-[24px] font-bold tracking-tight mb-2">Access token</h2>
      <p className="text-[15px] text-[#777] mb-10">Connect with a CloudBees Platform personal access token.</p>

      {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

      <form onSubmit={onSubmit} className="space-y-6">
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
            className="w-full h-11 px-4 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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
              className="w-full h-11 px-4 pr-11 rounded-xl border border-white/[0.06] bg-transparent text-[15px] placeholder:text-[#444] focus:outline-none focus:border-white/[0.12] transition-colors disabled:opacity-50"
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

        <div className="pt-4">
          <button
            type="submit"
            disabled={loading || !platformUrl || !pat}
            className="w-full h-11 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {loading && <Loader2 className="h-4 w-4 animate-spin" />}
            {loading ? "Connecting..." : "Connect"}
          </button>
        </div>
      </form>
    </div>
  );
}

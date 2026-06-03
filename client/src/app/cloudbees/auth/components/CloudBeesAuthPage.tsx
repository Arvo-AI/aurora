"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Check, ChevronDown, ChevronLeft, ExternalLink, Eye, EyeOff,
  Loader2, MonitorCog, Network, ShieldCheck, KeyRound,
} from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { cloudbeesService } from "@/lib/services/ci-provider";
import type { CIProviderStatus } from "@/lib/services/ci-provider";
import { apiRequest } from "@/lib/services/api-client";

type ConnectionMode = "single-controller" | "operations-center" | "personal-access-token";

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
  const [showFeatureManagement, setShowFeatureManagement] = useState(false);
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

  const isConnected = Boolean(status?.connected);

  if (checkingStatus && !status) {
    return (
      <div className="container mx-auto py-16 px-4 max-w-3xl flex flex-col items-center justify-center gap-3">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
        <p className="text-sm text-muted-foreground">Checking connection status...</p>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-3xl">
      <button
        onClick={() => router.push("/connectors")}
        className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors mb-6"
      >
        <ChevronLeft className="h-4 w-4" />
        Back to Connectors
      </button>

      <div className="flex items-center gap-4 mb-8">
        <div className="p-2 rounded-xl shadow-sm border overflow-hidden">
          <img src="/cloudbees.svg" alt="CloudBees" className="h-9 w-9 object-contain rounded-md" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">CloudBees CI</h1>
          <p className="text-muted-foreground text-sm">Read-only access to jobs, builds, pipelines, and agents</p>
        </div>
        {isConnected && (
          <Badge className="ml-auto bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 border-green-200 dark:border-green-800 hover:bg-green-100">
            <Check className="h-3 w-3 mr-1" />
            Connected
          </Badge>
        )}
      </div>

      {isConnected ? (
        <ConnectedSection
          status={status}
          controllers={controllers}
          loading={loading}
          onDisconnect={handleDisconnect}
        />
      ) : (
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">How would you like to connect?</CardTitle>
              <CardDescription>
                Choose the connection method that matches your CloudBees setup
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <ModeCard
                selected={mode === "single-controller"}
                onClick={() => setMode("single-controller")}
                icon={<MonitorCog className="h-5 w-5 text-violet-600" />}
                title="Single Controller"
                description="Connect directly to one Jenkins/CloudBees CI controller"
              />
              <ModeCard
                selected={mode === "operations-center"}
                onClick={() => setMode("operations-center")}
                icon={<Network className="h-5 w-5 text-violet-600" />}
                title="Operations Center"
                description="Connect to your CloudBees Operations Center to manage all controllers from one place"
              />
              <ModeCard
                selected={mode === "personal-access-token"}
                onClick={() => setMode("personal-access-token")}
                icon={<KeyRound className="h-5 w-5 text-violet-600" />}
                title="Personal Access Token"
                description="Use a CloudBees Platform PAT (if your org uses platform-level authentication)"
              />
            </CardContent>
          </Card>

          {urlError && (
            <p className="text-sm text-red-500 px-1">{urlError}</p>
          )}

          {mode === "single-controller" && (
            <SingleControllerForm
              baseUrl={baseUrl}
              setBaseUrl={setBaseUrl}
              username={username}
              setUsername={setUsername}
              apiToken={apiToken}
              setApiToken={setApiToken}
              showToken={showToken}
              setShowToken={setShowToken}
              loading={loading}
              onSubmit={handleSingleControllerConnect}
            />
          )}

          {mode === "operations-center" && (
            <OperationsCenterForm
              ocUrl={ocUrl}
              setOcUrl={setOcUrl}
              ocUsername={ocUsername}
              setOcUsername={setOcUsername}
              ocApiToken={ocApiToken}
              setOcApiToken={setOcApiToken}
              showOcToken={showOcToken}
              setShowOcToken={setShowOcToken}
              rolloutToken={rolloutToken}
              setRolloutToken={setRolloutToken}
              showRolloutToken={showRolloutToken}
              setShowRolloutToken={setShowRolloutToken}
              showFeatureManagement={showFeatureManagement}
              setShowFeatureManagement={setShowFeatureManagement}
              loading={loading}
              onSubmit={handleOCConnect}
            />
          )}

          {mode === "personal-access-token" && (
            <PATForm
              platformUrl={platformUrl}
              setPlatformUrl={setPlatformUrl}
              pat={pat}
              setPat={setPat}
              showPat={showPat}
              setShowPat={setShowPat}
              loading={loading}
              onSubmit={handlePATConnect}
            />
          )}
        </div>
      )}
    </div>
  );
}

function ModeCard({
  selected,
  onClick,
  icon,
  title,
  description,
}: {
  selected: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left p-4 rounded-lg border-2 transition-all ${
        selected
          ? "border-violet-600 bg-violet-50/50 dark:bg-violet-950/20 dark:border-violet-500"
          : "border-border hover:border-muted-foreground/30 hover:bg-muted/30"
      }`}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 shrink-0">{icon}</div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm">{title}</span>
            {selected && (
              <div className="h-4 w-4 rounded-full bg-violet-600 flex items-center justify-center">
                <Check className="h-2.5 w-2.5 text-white" />
              </div>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">{description}</p>
        </div>
      </div>
    </button>
  );
}

function SingleControllerForm({
  baseUrl, setBaseUrl, username, setUsername, apiToken, setApiToken,
  showToken, setShowToken, loading, onSubmit,
}: {
  baseUrl: string; setBaseUrl: (v: string) => void;
  username: string; setUsername: (v: string) => void;
  apiToken: string; setApiToken: (v: string) => void;
  showToken: boolean; setShowToken: (v: boolean) => void;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">Connect to Controller</CardTitle>
        <CardDescription>
          Enter your CloudBees CI controller URL and API credentials
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-5">
          <div className="grid gap-1.5">
            <Label htmlFor="sc-url" className="text-sm font-medium">Controller URL</Label>
            <Input id="sc-url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://cloudbees.example.com" required disabled={loading} className="h-10" />
            <p className="text-xs text-muted-foreground">Full URL to your CloudBees CI controller, without a trailing slash</p>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="sc-username" className="text-sm font-medium">Username</Label>
            <Input id="sc-username" value={username} onChange={(e) => setUsername(e.target.value)}
              placeholder="your-cloudbees-username" required disabled={loading} className="h-10" />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="sc-token" className="text-sm font-medium">API Token</Label>
            <div className="relative">
              <Input id="sc-token" type={showToken ? "text" : "password"} value={apiToken}
                onChange={(e) => setApiToken(e.target.value)} placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                required disabled={loading} className="h-10 pr-10" />
              <button type="button" onClick={() => setShowToken(!showToken)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                aria-label={showToken ? "Hide token" : "Show token"}>
                {showToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
            <p className="text-xs text-muted-foreground">
              Generate a token: Click your username &rarr; Security &rarr; API Token &rarr; Add new Token
            </p>
          </div>
          <SecurityNote />
          <Button type="submit" disabled={loading || !baseUrl || !username || !apiToken} className="w-full h-10">
            {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : "Connect Controller"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function OperationsCenterForm({
  ocUrl, setOcUrl, ocUsername, setOcUsername, ocApiToken, setOcApiToken,
  showOcToken, setShowOcToken, rolloutToken, setRolloutToken,
  showRolloutToken, setShowRolloutToken, showFeatureManagement, setShowFeatureManagement,
  loading, onSubmit,
}: {
  ocUrl: string; setOcUrl: (v: string) => void;
  ocUsername: string; setOcUsername: (v: string) => void;
  ocApiToken: string; setOcApiToken: (v: string) => void;
  showOcToken: boolean; setShowOcToken: (v: boolean) => void;
  rolloutToken: string; setRolloutToken: (v: string) => void;
  showRolloutToken: boolean; setShowRolloutToken: (v: boolean) => void;
  showFeatureManagement: boolean; setShowFeatureManagement: (v: boolean) => void;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">Connect to Operations Center</CardTitle>
        <CardDescription>
          Connect to your CloudBees Operations Center to discover and manage all controllers
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-5">
          <div className="grid gap-1.5">
            <Label htmlFor="oc-url" className="text-sm font-medium">Operations Center URL</Label>
            <Input id="oc-url" value={ocUrl} onChange={(e) => setOcUrl(e.target.value)}
              placeholder="https://cjoc.company.com" required disabled={loading} className="h-10" />
            <p className="text-xs text-muted-foreground">Full URL to your CloudBees Operations Center</p>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="oc-username" className="text-sm font-medium">Username</Label>
            <Input id="oc-username" value={ocUsername} onChange={(e) => setOcUsername(e.target.value)}
              placeholder="your-cloudbees-username" required disabled={loading} className="h-10" />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="oc-token" className="text-sm font-medium">API Token</Label>
            <div className="relative">
              <Input id="oc-token" type={showOcToken ? "text" : "password"} value={ocApiToken}
                onChange={(e) => setOcApiToken(e.target.value)} placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                required disabled={loading} className="h-10 pr-10" />
              <button type="button" onClick={() => setShowOcToken(!showOcToken)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                aria-label={showOcToken ? "Hide token" : "Show token"}>
                {showOcToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
            <p className="text-xs text-muted-foreground">
              Generate a token in your Operations Center: Click your username &rarr; Security &rarr; API Token &rarr; Add new Token
            </p>
          </div>

          {/* Feature Management (collapsible) */}
          <div className="border rounded-lg overflow-hidden">
            <button
              type="button"
              onClick={() => setShowFeatureManagement(!showFeatureManagement)}
              className="w-full text-left p-3 flex items-center justify-between hover:bg-muted/50 transition-colors"
            >
              <span className="text-sm font-medium text-muted-foreground">Feature Management (optional)</span>
              <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${showFeatureManagement ? "rotate-180" : ""}`} />
            </button>
            {showFeatureManagement && (
              <div className="px-3 pb-3 border-t pt-3 space-y-1.5">
                <Label htmlFor="rollout-token" className="text-sm font-medium">Rollout API Token</Label>
                <div className="relative">
                  <Input id="rollout-token" type={showRolloutToken ? "text" : "password"} value={rolloutToken}
                    onChange={(e) => setRolloutToken(e.target.value)} placeholder="Bearer token from Feature Management settings"
                    disabled={loading} className="h-10 pr-10" />
                  <button type="button" onClick={() => setShowRolloutToken(!showRolloutToken)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                    aria-label={showRolloutToken ? "Hide token" : "Show token"}>
                    {showRolloutToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Optional &mdash; enables feature flag change correlation during incident investigation
                </p>
              </div>
            )}
          </div>

          <SecurityNote />
          <Button type="submit" disabled={loading || !ocUrl || !ocUsername || !ocApiToken} className="w-full h-10">
            {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : "Connect Operations Center"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function PATForm({
  platformUrl, setPlatformUrl, pat, setPat, showPat, setShowPat, loading, onSubmit,
}: {
  platformUrl: string; setPlatformUrl: (v: string) => void;
  pat: string; setPat: (v: string) => void;
  showPat: boolean; setShowPat: (v: boolean) => void;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">Connect with Personal Access Token</CardTitle>
        <CardDescription>
          Use a CloudBees Platform PAT for organizations using platform-level authentication
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-5">
          <div className="grid gap-1.5">
            <Label htmlFor="pat-url" className="text-sm font-medium">Platform URL</Label>
            <Input id="pat-url" value={platformUrl} onChange={(e) => setPlatformUrl(e.target.value)}
              placeholder="https://your-org.cloudbees.io" required disabled={loading} className="h-10" />
            <p className="text-xs text-muted-foreground">Your CloudBees Platform URL</p>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="pat-token" className="text-sm font-medium">Personal Access Token</Label>
            <div className="relative">
              <Input id="pat-token" type={showPat ? "text" : "password"} value={pat}
                onChange={(e) => setPat(e.target.value)} placeholder="cbp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                required disabled={loading} className="h-10 pr-10" />
              <button type="button" onClick={() => setShowPat(!showPat)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                aria-label={showPat ? "Hide token" : "Show token"}>
                {showPat ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
            <p className="text-xs text-muted-foreground">
              Generate a PAT: Click your profile &rarr; User profile &rarr; Personal access tokens &rarr; Create token
            </p>
          </div>
          <SecurityNote />
          <Button type="submit" disabled={loading || !platformUrl || !pat} className="w-full h-10">
            {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : "Connect with PAT"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function SecurityNote() {
  return (
    <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
      <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
      <div className="space-y-1">
        <p className="font-medium">Secure &amp; Read-Only</p>
        <p className="text-muted-foreground">
          Your credentials are encrypted and stored in Vault. Aurora only reads job and build data &mdash; it cannot trigger builds or modify configuration.
        </p>
      </div>
    </div>
  );
}

function ConnectedSection({
  status,
  controllers,
  loading,
  onDisconnect,
}: {
  status: CIProviderStatus | null;
  controllers: DiscoveredController[];
  loading: boolean;
  onDisconnect: () => void;
}) {
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="pt-6 space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium truncate">{status?.baseUrl}</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                {status?.username}
                {status?.server?.version ? ` · v${status.server.version}` : ""}
                {status?.server?.mode ? ` · ${status.server.mode.charAt(0).toUpperCase()}${status.server.mode.slice(1).toLowerCase()}` : ""}
              </p>
            </div>
            {status?.baseUrl && (
              <a href={status.baseUrl} target="_blank" rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors shrink-0">
                Open Dashboard
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        </CardContent>
      </Card>

      {controllers.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Network className="h-5 w-5 text-violet-600" />
              <CardTitle className="text-lg">Discovered Controllers</CardTitle>
            </div>
            <CardDescription>
              {controllers.length} controller{controllers.length !== 1 ? "s" : ""} found via Operations Center
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {controllers.map((ctrl) => (
                <div key={ctrl.url} className="flex items-center justify-between p-3 rounded-lg border bg-card hover:bg-muted/50 transition-colors">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium truncate">{ctrl.name}</p>
                    <p className="text-xs text-muted-foreground truncate">{ctrl.url}</p>
                  </div>
                  <Badge
                    variant={ctrl.status === "online" ? "default" : "secondary"}
                    className={ctrl.status === "online" ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 border-green-200 dark:border-green-800" : ""}
                  >
                    {ctrl.status}
                  </Badge>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex items-center justify-between px-1">
        <p className="text-xs text-muted-foreground">Remove stored credentials and disconnect</p>
        <Button variant="ghost" size="sm" onClick={onDisconnect} disabled={loading}
          className="text-red-500 hover:text-red-600 hover:bg-red-500/10 dark:text-red-400 dark:hover:text-red-300 h-8 text-xs">
          {loading ? <Loader2 className="h-3 w-3 animate-spin mr-1.5" /> : null}
          {loading ? "Disconnecting..." : "Disconnect"}
        </Button>
      </div>
    </div>
  );
}

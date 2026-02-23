"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { jenkinsService, JenkinsStatus } from "@/lib/services/jenkins";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Check,
  ChevronLeft,
  CircleDot,
  ExternalLink,
  Eye,
  EyeOff,
  Loader2,
  Monitor,
  ShieldCheck,
  Cpu,
  Globe,
  User,
  Hash,
  Briefcase,
  Server,
  Clock,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  MinusCircle,
} from "lucide-react";

const getUserFriendlyError = (err: unknown): string => {
  if (!err) return "An unexpected error occurred. Please try again.";
  if (typeof err === "string") return err;
  if (err instanceof Error) {
    const { message } = err;
    if (!message) return "An unexpected error occurred. Please try again.";
    try {
      const parsed = JSON.parse(message) as { error?: string };
      return parsed.error ?? message;
    } catch {
      return message;
    }
  }
  if (typeof err === "object") {
    const errorValue = (err as Record<string, unknown>).error;
    if (typeof errorValue === "string") return errorValue;
  }
  return "An unexpected error occurred. Please try again.";
};

const CACHE_KEY = "jenkins_connection_status";

export default function JenkinsAuthPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [username, setUsername] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [status, setStatus] = useState<JenkinsStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [expandedStep, setExpandedStep] = useState<number | null>(1);

  const toggleStep = (step: number) => {
    setExpandedStep(expandedStep === step ? null : step);
  };

  const loadStatus = async () => {
    setCheckingStatus(true);
    try {
      // Show cached status immediately so the page doesn't flash
      let hasCachedConnected = false;
      if (typeof window !== "undefined") {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          const parsed = JSON.parse(cached);
          setStatus(parsed);
          if (parsed?.connected) {
            hasCachedConnected = true;
            setBaseUrl(parsed.baseUrl ?? "");
            setUsername(parsed.username ?? "");
          }
        }
      }

      // Refresh from API in background
      const result = await jenkinsService.getStatus();

      // Only update if we got a valid response; don't overwrite
      // a known-good cache with a failed API call
      if (result) {
        setStatus(result);
        if (typeof window !== "undefined") {
          localStorage.setItem(CACHE_KEY, JSON.stringify(result));
          if (result.connected) {
            localStorage.setItem("isJenkinsConnected", "true");
          } else {
            localStorage.removeItem("isJenkinsConnected");
          }
        }
        if (result.connected) {
          setBaseUrl(result.baseUrl ?? "");
          setUsername(result.username ?? "");
        }
      }
    } catch (err) {
      console.error("Failed to load Jenkins status", err);
    } finally {
      setCheckingStatus(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);

    try {
      const result = await jenkinsService.connect({ baseUrl, username, apiToken });
      setStatus(result);

      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        localStorage.setItem("isJenkinsConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "jenkins" }),
        });
      } catch {
        // preferences update is best-effort
      }

      // Fetch full status (with summary data) now that credentials are stored
      await loadStatus();

      toast({
        title: "Jenkins Connected",
        description: `Successfully connected to ${result.baseUrl || "Jenkins"}`,
      });
    } catch (err: unknown) {
      console.error("Jenkins connection failed", err);
      toast({
        title: "Connection Failed",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
      setApiToken("");
      setShowToken(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/jenkins", {
        method: "DELETE",
        credentials: "include",
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Jenkins");
      }

      setStatus({ connected: false });
      setBaseUrl("");
      setUsername("");

      if (typeof window !== "undefined") {
        localStorage.removeItem(CACHE_KEY);
        localStorage.removeItem("isJenkinsConnected");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "remove", provider: "jenkins" }),
        });
      } catch {
        // preferences update is best-effort
      }

      toast({ title: "Disconnected", description: "Jenkins has been disconnected." });
    } catch (err: unknown) {
      console.error("Jenkins disconnect failed", err);
      toast({
        title: "Disconnect Failed",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
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
      {/* Back navigation */}
      <button
        onClick={() => router.push("/connectors")}
        className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors mb-6"
      >
        <ChevronLeft className="h-4 w-4" />
        Back to Connectors
      </button>

      {/* Header with logo */}
      <div className="flex items-center gap-4 mb-8">
        <div className="p-2.5 rounded-xl bg-white dark:bg-white shadow-sm border">
          <img src="/jenkins.svg" alt="Jenkins" className="h-8 w-8 object-contain" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Jenkins</h1>
          <p className="text-muted-foreground text-sm">
            Read-only access to jobs, builds, pipelines, and agents
          </p>
        </div>
        {isConnected && (
          <Badge className="ml-auto bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 border-green-200 dark:border-green-800 hover:bg-green-100">
            <Check className="h-3 w-3 mr-1" />
            Connected
          </Badge>
        )}
      </div>

      {isConnected ? (
        <div className="space-y-6">
          {/* Summary stats row */}
          {status?.summary && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <Card className="p-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-blue-100 dark:bg-blue-900/30">
                    <Briefcase className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                  </div>
                  <div>
                    <p className="text-2xl font-bold leading-none">{status.summary.jobCount}</p>
                    <p className="text-xs text-muted-foreground mt-1">Jobs</p>
                  </div>
                </div>
              </Card>
              <Card className="p-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-green-100 dark:bg-green-900/30">
                    <Server className="h-4 w-4 text-green-600 dark:text-green-400" />
                  </div>
                  <div>
                    <p className="text-2xl font-bold leading-none">
                      {status.summary.nodesOnline}
                      <span className="text-sm font-normal text-muted-foreground">/{status.summary.nodesOnline + status.summary.nodesOffline}</span>
                    </p>
                    <p className="text-xs text-muted-foreground mt-1">Nodes Online</p>
                  </div>
                </div>
              </Card>
              <Card className="p-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-purple-100 dark:bg-purple-900/30">
                    <Cpu className="h-4 w-4 text-purple-600 dark:text-purple-400" />
                  </div>
                  <div>
                    <p className="text-2xl font-bold leading-none">
                      {status.summary.busyExecutors}
                      <span className="text-sm font-normal text-muted-foreground">/{status.summary.totalExecutors}</span>
                    </p>
                    <p className="text-xs text-muted-foreground mt-1">Executors Busy</p>
                  </div>
                </div>
              </Card>
              <Card className="p-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-orange-100 dark:bg-orange-900/30">
                    <Clock className="h-4 w-4 text-orange-600 dark:text-orange-400" />
                  </div>
                  <div>
                    <p className="text-2xl font-bold leading-none">{status.summary.queueSize}</p>
                    <p className="text-xs text-muted-foreground mt-1">In Queue</p>
                  </div>
                </div>
              </Card>
            </div>
          )}

          {/* Job health breakdown */}
          {status?.summary && status.summary.jobCount > 0 && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-lg">Job Health</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-3">
                  {/* Health bar */}
                  <div className="flex h-3 rounded-full overflow-hidden bg-muted">
                    {status.summary.jobHealth.healthy > 0 && (
                      <div
                        className="bg-green-500 transition-all duration-500"
                        style={{ width: `${(status.summary.jobHealth.healthy / status.summary.jobCount) * 100}%` }}
                      />
                    )}
                    {status.summary.jobHealth.unstable > 0 && (
                      <div
                        className="bg-yellow-500 transition-all duration-500"
                        style={{ width: `${(status.summary.jobHealth.unstable / status.summary.jobCount) * 100}%` }}
                      />
                    )}
                    {status.summary.jobHealth.failing > 0 && (
                      <div
                        className="bg-red-500 transition-all duration-500"
                        style={{ width: `${(status.summary.jobHealth.failing / status.summary.jobCount) * 100}%` }}
                      />
                    )}
                    {status.summary.jobHealth.disabled > 0 && (
                      <div
                        className="bg-gray-400 transition-all duration-500"
                        style={{ width: `${(status.summary.jobHealth.disabled / status.summary.jobCount) * 100}%` }}
                      />
                    )}
                    {status.summary.jobHealth.other > 0 && (
                      <div
                        className="bg-gray-300 transition-all duration-500"
                        style={{ width: `${(status.summary.jobHealth.other / status.summary.jobCount) * 100}%` }}
                      />
                    )}
                  </div>

                  {/* Legend */}
                  <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
                    {status.summary.jobHealth.healthy > 0 && (
                      <div className="flex items-center gap-1.5">
                        <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
                        <span className="text-muted-foreground">Passing</span>
                        <span className="font-semibold">{status.summary.jobHealth.healthy}</span>
                      </div>
                    )}
                    {status.summary.jobHealth.unstable > 0 && (
                      <div className="flex items-center gap-1.5">
                        <AlertTriangle className="h-3.5 w-3.5 text-yellow-500" />
                        <span className="text-muted-foreground">Unstable</span>
                        <span className="font-semibold">{status.summary.jobHealth.unstable}</span>
                      </div>
                    )}
                    {status.summary.jobHealth.failing > 0 && (
                      <div className="flex items-center gap-1.5">
                        <XCircle className="h-3.5 w-3.5 text-red-500" />
                        <span className="text-muted-foreground">Failing</span>
                        <span className="font-semibold">{status.summary.jobHealth.failing}</span>
                      </div>
                    )}
                    {status.summary.jobHealth.disabled > 0 && (
                      <div className="flex items-center gap-1.5">
                        <MinusCircle className="h-3.5 w-3.5 text-gray-400" />
                        <span className="text-muted-foreground">Disabled</span>
                        <span className="font-semibold">{status.summary.jobHealth.disabled}</span>
                      </div>
                    )}
                    {status.summary.jobHealth.other > 0 && (
                      <div className="flex items-center gap-1.5">
                        <CircleDot className="h-3.5 w-3.5 text-gray-300" />
                        <span className="text-muted-foreground">Other</span>
                        <span className="font-semibold">{status.summary.jobHealth.other}</span>
                      </div>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {/* Connection details */}
          <Card>
            <CardHeader className="pb-4">
              <CardTitle className="text-lg">Connection Details</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div className="flex items-start gap-3 p-3 rounded-lg bg-muted/50">
                    <Globe className="h-4 w-4 mt-0.5 text-muted-foreground shrink-0" />
                    <div className="min-w-0">
                      <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">URL</p>
                      <p className="text-sm font-medium truncate mt-0.5">{status?.baseUrl}</p>
                    </div>
                  </div>
                  <div className="flex items-start gap-3 p-3 rounded-lg bg-muted/50">
                    <User className="h-4 w-4 mt-0.5 text-muted-foreground shrink-0" />
                    <div className="min-w-0">
                      <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Username</p>
                      <p className="text-sm font-medium mt-0.5">{status?.username}</p>
                    </div>
                  </div>
                  {status?.server?.version && (
                    <div className="flex items-start gap-3 p-3 rounded-lg bg-muted/50">
                      <Hash className="h-4 w-4 mt-0.5 text-muted-foreground shrink-0" />
                      <div className="min-w-0">
                        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Version</p>
                        <p className="text-sm font-medium mt-0.5">{status.server.version}</p>
                      </div>
                    </div>
                  )}
                  {status?.server?.mode && (
                    <div className="flex items-start gap-3 p-3 rounded-lg bg-muted/50">
                      <Monitor className="h-4 w-4 mt-0.5 text-muted-foreground shrink-0" />
                      <div className="min-w-0">
                        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Mode</p>
                        <p className="text-sm font-medium capitalize mt-0.5">{status.server.mode}</p>
                      </div>
                    </div>
                  )}
                </div>

                {status?.baseUrl && (
                  <a
                    href={status.baseUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-sm text-blue-600 dark:text-blue-400 hover:underline"
                  >
                    Open Jenkins Dashboard
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                )}
              </div>
            </CardContent>
          </Card>

          {/* Danger zone */}
          <Card className="border-red-200 dark:border-red-900/50">
            <CardContent className="pt-6">
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-medium text-sm">Disconnect Jenkins</p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Remove stored credentials and disconnect this Jenkins instance
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDisconnect}
                  disabled={loading}
                  className="text-red-600 border-red-200 hover:bg-red-50 hover:text-red-700 dark:text-red-400 dark:border-red-800 dark:hover:bg-red-950/30"
                >
                  {loading ? (
                    <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
                  ) : null}
                  {loading ? "Disconnecting..." : "Disconnect"}
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      ) : (
        <div className="space-y-6">
          {/* Setup guide */}
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Connect Your Jenkins Instance</CardTitle>
              <CardDescription>
                Follow these steps to generate an API token and connect Jenkins to Aurora
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Step 1 */}
              <div className="border rounded-lg overflow-hidden">
                <button
                  onClick={() => toggleStep(1)}
                  className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-orange-600 text-white text-sm font-bold shrink-0">
                      1
                    </div>
                    <span className="font-medium">Generate an API Token in Jenkins</span>
                  </div>
                  <svg
                    className={`w-5 h-5 text-muted-foreground transition-transform duration-200 ${expandedStep === 1 ? "rotate-180" : ""}`}
                    fill="none" stroke="currentColor" viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>

                {expandedStep === 1 && (
                  <div className="px-4 pb-4 space-y-3 text-sm border-t pt-4">
                    <div className="space-y-2.5">
                      <div className="flex items-start gap-2.5">
                        <span className="text-muted-foreground font-mono text-xs mt-0.5 w-4 text-right shrink-0">1.</span>
                        <p>Sign in to your Jenkins instance as the user you want to connect</p>
                      </div>
                      <div className="flex items-start gap-2.5">
                        <span className="text-muted-foreground font-mono text-xs mt-0.5 w-4 text-right shrink-0">2.</span>
                        <div>
                          <p>Click your username in the top-right corner, then go to:</p>
                          <code className="block px-3 py-2 bg-muted rounded text-xs mt-1.5 font-mono">
                            Configure → API Token → Add new Token
                          </code>
                        </div>
                      </div>
                      <div className="flex items-start gap-2.5">
                        <span className="text-muted-foreground font-mono text-xs mt-0.5 w-4 text-right shrink-0">3.</span>
                        <p>Give the token a name (e.g. <strong>Aurora</strong>) and click <strong>Generate</strong></p>
                      </div>
                      <div className="flex items-start gap-2.5">
                        <span className="text-muted-foreground font-mono text-xs mt-0.5 w-4 text-right shrink-0">4.</span>
                        <p className="text-orange-700 dark:text-orange-400 font-medium">
                          Copy the token immediately &mdash; it won't be shown again
                        </p>
                      </div>
                    </div>

                    <div className="mt-3 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded-lg">
                      <a
                        href="https://www.jenkins.io/doc/book/system-administration/authenticating-scripted-clients/"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
                      >
                        Jenkins API Authentication Docs
                        <ExternalLink className="w-3 h-3" />
                      </a>
                    </div>
                  </div>
                )}
              </div>

              {/* Step 2 */}
              <div className="border rounded-lg overflow-hidden">
                <button
                  onClick={() => toggleStep(2)}
                  className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-orange-600 text-white text-sm font-bold shrink-0">
                      2
                    </div>
                    <span className="font-medium">Enter Your Credentials</span>
                  </div>
                  <svg
                    className={`w-5 h-5 text-muted-foreground transition-transform duration-200 ${expandedStep === 2 ? "rotate-180" : ""}`}
                    fill="none" stroke="currentColor" viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>

                {expandedStep === 2 && (
                  <div className="px-4 pb-4 border-t pt-4">
                    <form onSubmit={handleConnect} className="space-y-5">
                      <div className="grid gap-1.5">
                        <Label htmlFor="jenkins-url" className="text-sm font-medium">
                          Jenkins URL
                        </Label>
                        <Input
                          id="jenkins-url"
                          value={baseUrl}
                          onChange={(e) => setBaseUrl(e.target.value)}
                          placeholder="https://jenkins.example.com"
                          required
                          disabled={loading}
                          className="h-10"
                        />
                        <p className="text-xs text-muted-foreground">
                          Full URL to your Jenkins instance, without a trailing slash
                        </p>
                      </div>

                      <div className="grid gap-1.5">
                        <Label htmlFor="jenkins-username" className="text-sm font-medium">
                          Username
                        </Label>
                        <Input
                          id="jenkins-username"
                          value={username}
                          onChange={(e) => setUsername(e.target.value)}
                          placeholder="your-jenkins-username"
                          required
                          disabled={loading}
                          className="h-10"
                        />
                      </div>

                      <div className="grid gap-1.5">
                        <Label htmlFor="jenkins-token" className="text-sm font-medium">
                          API Token
                        </Label>
                        <div className="relative">
                          <Input
                            id="jenkins-token"
                            type={showToken ? "text" : "password"}
                            value={apiToken}
                            onChange={(e) => setApiToken(e.target.value)}
                            placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                            required
                            disabled={loading}
                            className="h-10 pr-10"
                          />
                          <button
                            type="button"
                            onClick={() => setShowToken(!showToken)}
                            className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                            tabIndex={-1}
                          >
                            {showToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                          </button>
                        </div>
                      </div>

                      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
                        <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
                        <div className="space-y-1">
                          <p className="font-medium">Secure & Read-Only</p>
                          <p className="text-muted-foreground">
                            Your credentials are encrypted and stored in Vault. Aurora only reads job and build data &mdash; it cannot trigger builds or modify configuration.
                          </p>
                        </div>
                      </div>

                      <Button
                        type="submit"
                        disabled={loading || !baseUrl || !username || !apiToken}
                        className="w-full h-10"
                      >
                        {loading ? (
                          <>
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            Connecting...
                          </>
                        ) : (
                          "Connect Jenkins"
                        )}
                      </Button>
                    </form>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

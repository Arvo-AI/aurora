"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { jenkinsService, JenkinsStatus, JenkinsWebhookInfo, JenkinsDeploymentEvent } from "@/lib/services/jenkins";
import { formatTimeAgo, formatDuration } from "@/lib/utils/time-format";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Check,
  ChevronLeft,
  Copy,
  ExternalLink,
  Eye,
  EyeOff,
  Loader2,
  Rocket,
  ShieldCheck,
  Webhook,
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
  const [webhookInfo, setWebhookInfo] = useState<JenkinsWebhookInfo | null>(null);
  const [deployments, setDeployments] = useState<JenkinsDeploymentEvent[]>([]);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const toggleStep = (step: number) => {
    setExpandedStep(expandedStep === step ? null : step);
  };

  const loadConnectedData = async () => {
    try {
      const [info, depData] = await Promise.allSettled([
        jenkinsService.getWebhookUrl(),
        jenkinsService.getDeployments(10),
      ]);
      if (info.status === "fulfilled" && info.value) {
        setWebhookInfo(info.value);
      } else if (info.status === "rejected") {
        console.error("[Jenkins] Failed to load webhook URL:", info.reason);
        toast({ title: "Failed to load webhook config", variant: "destructive" });
      }
      if (depData.status === "fulfilled" && depData.value) {
        setDeployments(depData.value.deployments);
      }
    } catch (err) {
      console.error("[Jenkins] Failed to load connected data:", err);
    }
  };

  const loadStatus = async () => {
    setCheckingStatus(true);
    try {
      const cached = localStorage.getItem(CACHE_KEY);
      if (cached) {
        const parsed = JSON.parse(cached);
        setStatus(parsed);
        if (parsed?.connected) {
          setBaseUrl(parsed.baseUrl ?? "");
          setUsername(parsed.username ?? "");
        }
      }

      const result = await jenkinsService.getStatus();

      if (result) {
        setStatus(result);
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        if (result.connected) {
          localStorage.setItem("isJenkinsConnected", "true");
          loadConnectedData();
        } else {
          localStorage.removeItem("isJenkinsConnected");
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

  const copyToClipboard = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 2000);
    } catch {
      toast({ title: "Failed to copy", description: "Clipboard access denied. Copy manually.", variant: "destructive" });
    }
  };

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);

    try {
      await jenkinsService.connect({ baseUrl, username, apiToken });

      localStorage.setItem("isJenkinsConnected", "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "jenkins" }),
        });
      } catch {
        // preferences update is best-effort
      }

      await loadStatus();

      toast({
        title: "Jenkins Connected",
        description: `Successfully connected to ${baseUrl || "Jenkins"}`,
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

      localStorage.removeItem(CACHE_KEY);
      localStorage.removeItem("isJenkinsConnected");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

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
        <div className="space-y-4">
          {/* Single overview card */}
          <Card>
            <CardContent className="pt-6 space-y-6">
              {/* Connection info row */}
              <div className="flex items-center justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate">{status?.baseUrl}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {status?.username}{status?.server?.version ? ` \u00b7 v${status.server.version}` : ""}{status?.server?.mode ? ` \u00b7 ${status.server.mode.charAt(0).toUpperCase()}${status.server.mode.slice(1).toLowerCase()}` : ""}
                  </p>
                </div>
                {status?.baseUrl && (
                  <a
                    href={status.baseUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors shrink-0"
                  >
                    Open Dashboard
                    <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>

              {/* Stats row */}
              {status?.summary && (
                <>
                  <div className="border-t" />
                  <div className="grid grid-cols-4 gap-2 text-center">
                    <div>
                      <p className="text-2xl font-semibold tabular-nums">{status.summary.jobCount}</p>
                      <p className="text-[11px] text-muted-foreground mt-0.5">Jobs</p>
                    </div>
                    <div>
                      <p className="text-2xl font-semibold tabular-nums">
                        {status.summary.nodesOnline}
                        <span className="text-sm font-normal text-muted-foreground">/{status.summary.nodesOnline + status.summary.nodesOffline}</span>
                      </p>
                      <p className="text-[11px] text-muted-foreground mt-0.5">Nodes</p>
                    </div>
                    <div>
                      <p className="text-2xl font-semibold tabular-nums">
                        {status.summary.busyExecutors}
                        <span className="text-sm font-normal text-muted-foreground">/{status.summary.totalExecutors}</span>
                      </p>
                      <p className="text-[11px] text-muted-foreground mt-0.5">Executors</p>
                    </div>
                    <div>
                      <p className="text-2xl font-semibold tabular-nums">{status.summary.queueSize}</p>
                      <p className="text-[11px] text-muted-foreground mt-0.5">Queued</p>
                    </div>
                  </div>
                </>
              )}

              {/* Job health */}
              {status?.summary && status.summary.jobCount > 0 && (
                <>
                  <div className="border-t" />
                  <div className="space-y-2.5">
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Job Health</p>
                    <div className="flex h-2 rounded-full overflow-hidden bg-muted">
                      {status.summary.jobHealth.healthy > 0 && (
                        <div className="bg-green-500" style={{ width: `${(status.summary.jobHealth.healthy / status.summary.jobCount) * 100}%` }} />
                      )}
                      {status.summary.jobHealth.unstable > 0 && (
                        <div className="bg-yellow-500" style={{ width: `${(status.summary.jobHealth.unstable / status.summary.jobCount) * 100}%` }} />
                      )}
                      {status.summary.jobHealth.failing > 0 && (
                        <div className="bg-red-500" style={{ width: `${(status.summary.jobHealth.failing / status.summary.jobCount) * 100}%` }} />
                      )}
                      {status.summary.jobHealth.disabled > 0 && (
                        <div className="bg-muted-foreground/30" style={{ width: `${(status.summary.jobHealth.disabled / status.summary.jobCount) * 100}%` }} />
                      )}
                      {status.summary.jobHealth.other > 0 && (
                        <div className="bg-muted-foreground/15" style={{ width: `${(status.summary.jobHealth.other / status.summary.jobCount) * 100}%` }} />
                      )}
                    </div>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                      {status.summary.jobHealth.healthy > 0 && (
                        <span className="flex items-center gap-1.5">
                          <span className="h-2 w-2 rounded-full bg-green-500" />
                          {status.summary.jobHealth.healthy} Passing
                        </span>
                      )}
                      {status.summary.jobHealth.unstable > 0 && (
                        <span className="flex items-center gap-1.5">
                          <span className="h-2 w-2 rounded-full bg-yellow-500" />
                          {status.summary.jobHealth.unstable} Unstable
                        </span>
                      )}
                      {status.summary.jobHealth.failing > 0 && (
                        <span className="flex items-center gap-1.5">
                          <span className="h-2 w-2 rounded-full bg-red-500" />
                          {status.summary.jobHealth.failing} Failing
                        </span>
                      )}
                      {status.summary.jobHealth.disabled > 0 && (
                        <span className="flex items-center gap-1.5">
                          <span className="h-2 w-2 rounded-full bg-muted-foreground/30" />
                          {status.summary.jobHealth.disabled} Disabled
                        </span>
                      )}
                      {status.summary.jobHealth.other > 0 && (
                        <span className="flex items-center gap-1.5">
                          <span className="h-2 w-2 rounded-full bg-muted-foreground/15" />
                          {status.summary.jobHealth.other} Other
                        </span>
                      )}
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          {/* Webhook Configuration Card */}
          {webhookInfo && (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-center gap-2">
                  <Webhook className="h-5 w-5 text-orange-600" />
                  <CardTitle className="text-lg">Send Deployment Events to Aurora</CardTitle>
                </div>
                <CardDescription>
                  Add this to your Jenkinsfile to notify Aurora when builds complete
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                {/* Main snippet - curl by default */}
                <div className="relative">
                  <pre className="text-xs bg-muted p-3 rounded-lg whitespace-pre-wrap break-all pr-20">
                    <code>{webhookInfo.jenkinsfileCurl}</code>
                  </pre>
                  <Button
                    size="sm"
                    variant="secondary"
                    className="absolute top-2 right-2 h-8 gap-1.5"
                    onClick={() => copyToClipboard(webhookInfo.jenkinsfileCurl, 'curl')}
                  >
                    {copiedKey === 'curl' ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                    {copiedKey === 'curl' ? "Copied!" : "Copy"}
                  </Button>
                </div>

                {/* Alternative snippets - collapsed */}
                <details className="text-xs group">
                  <summary className="cursor-pointer text-muted-foreground hover:text-foreground flex items-center gap-1">
                    <svg className="h-3 w-3 transition-transform group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                    </svg>
                    Alternative snippets (with plugins)
                  </summary>
                  <div className="mt-3 space-y-3 pl-4 border-l-2 border-muted">
                    {/* httpRequest option */}
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="font-medium">With HTTP Request Plugin</span>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-6 text-xs gap-1"
                          onClick={() => copyToClipboard(webhookInfo.jenkinsfileBasic, 'basic')}
                        >
                          <Copy className="h-3 w-3" />
                          Copy
                        </Button>
                      </div>
                      <p className="text-muted-foreground mb-2">
                        Better error handling. Requires{" "}
                        <a href="https://plugins.jenkins.io/http_request/" target="_blank" rel="noopener noreferrer" className="underline hover:text-foreground">
                          HTTP Request Plugin
                        </a>
                      </p>
                      <pre className="bg-muted/50 p-2 rounded text-[11px] whitespace-pre-wrap break-all">
                        <code>{webhookInfo.jenkinsfileBasic}</code>
                      </pre>
                    </div>

                    {/* OTel option */}
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="font-medium">With Trace Correlation</span>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-6 text-xs gap-1"
                          onClick={() => copyToClipboard(webhookInfo.jenkinsfileOtel, 'otel')}
                        >
                          <Copy className="h-3 w-3" />
                          Copy
                        </Button>
                      </div>
                      <p className="text-muted-foreground mb-2">
                        Links Jenkins builds to application traces. Requires{" "}
                        <a href="https://plugins.jenkins.io/http_request/" target="_blank" rel="noopener noreferrer" className="underline hover:text-foreground">
                          HTTP Request
                        </a>
                        {" + "}
                        <a href="https://plugins.jenkins.io/opentelemetry/" target="_blank" rel="noopener noreferrer" className="underline hover:text-foreground">
                          OpenTelemetry
                        </a>
                        {" plugins"}
                      </p>
                      <pre className="bg-muted/50 p-2 rounded text-[11px] whitespace-pre-wrap break-all">
                        <code>{webhookInfo.jenkinsfileOtel}</code>
                      </pre>
                    </div>
                  </div>
                </details>
              </CardContent>
            </Card>
          )}

          {/* Recent Deployments Card */}
          {deployments.length > 0 && (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-center gap-2">
                  <Rocket className="h-5 w-5 text-orange-600" />
                  <CardTitle className="text-lg">Recent Deployments</CardTitle>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {deployments.map((dep) => {
                    const timeAgo = dep.receivedAt ? formatTimeAgo(new Date(dep.receivedAt)) : null;
                    const duration = dep.durationMs ? formatDuration(dep.durationMs) : null;
                    
                    return (
                      <div key={dep.id} className="flex items-start justify-between p-3 rounded-lg border bg-card hover:bg-muted/50 transition-colors">
                        <div className="flex-1 min-w-0 space-y-1">
                          {/* Row 1: Result + Service + Environment */}
                          <div className="flex items-center gap-2 flex-wrap">
                            <Badge 
                              variant={dep.result === "SUCCESS" ? "default" : dep.result === "FAILURE" ? "destructive" : "secondary"} 
                              className="h-5 text-xs shrink-0"
                            >
                              {dep.result || "UNKNOWN"}
                            </Badge>
                            <span className="font-medium text-sm">{dep.service}</span>
                            <span className="text-xs text-muted-foreground">→ {dep.environment}</span>
                          </div>
                          {/* Row 2: Build # + Duration + Time + Deployer */}
                          <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
                            <span className="font-mono">#{dep.buildNumber}</span>
                            {duration && (
                              <>
                                <span>•</span>
                                <span>{duration}</span>
                              </>
                            )}
                            {timeAgo && (
                              <>
                                <span>•</span>
                                <span>{timeAgo}</span>
                              </>
                            )}
                            {dep.deployer && dep.deployer !== "automated" && (
                              <>
                                <span>•</span>
                                <span>by {dep.deployer}</span>
                              </>
                            )}
                          </div>
                        </div>
                        {dep.buildUrl && dep.buildUrl.startsWith("http") && (
                          <a
                            href={dep.buildUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="ml-3 p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded transition-colors shrink-0"
                            title="View in Jenkins"
                          >
                            <ExternalLink className="h-4 w-4" />
                          </a>
                        )}
                      </div>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          )}

          {/* Disconnect */}
          <div className="flex items-center justify-between px-1">
            <p className="text-xs text-muted-foreground">
              Remove stored credentials and disconnect
            </p>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleDisconnect}
              disabled={loading}
              className="text-red-500 hover:text-red-600 hover:bg-red-500/10 dark:text-red-400 dark:hover:text-red-300 h-8 text-xs"
            >
              {loading ? (
                <Loader2 className="h-3 w-3 animate-spin mr-1.5" />
              ) : null}
              {loading ? "Disconnecting..." : "Disconnect"}
            </Button>
          </div>
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

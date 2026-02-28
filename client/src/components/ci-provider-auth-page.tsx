"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import type { CIProviderConfig, CIProviderStatus, CIWebhookInfo, CIDeploymentEvent, CIRcaSettings } from "@/lib/services/ci-provider";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import {
  Check, ChevronLeft, Copy, ExternalLink, Eye, EyeOff,
  Loader2, Rocket, ShieldCheck, Webhook, Zap,
} from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { formatTimeAgo, formatDuration } from "@/lib/utils/time-format";

const toSafeExternalUrl = (value?: string): string | null => {
  if (!value) return null;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:" ? u.toString() : null;
  } catch {
    return null;
  }
};

export default function CIProviderAuthPage({ config }: { config: CIProviderConfig }) {
  const router = useRouter();
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [username, setUsername] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [status, setStatus] = useState<CIProviderStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [expandedStep, setExpandedStep] = useState<number | null>(1);
  const [webhookInfo, setWebhookInfo] = useState<CIWebhookInfo | null>(null);
  const [deployments, setDeployments] = useState<CIDeploymentEvent[]>([]);
  const [rcaSettings, setRcaSettings] = useState<CIRcaSettings | null>(null);
  const [rcaToggleLoading, setRcaToggleLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const { slug, displayName, service, cacheKey, localStorageConnectedKey } = config;

  const toggleStep = (step: number) => {
    setExpandedStep(expandedStep === step ? null : step);
  };

  const loadStatus = async () => {
    setCheckingStatus(true);
    try {
      try {
        const cached = localStorage.getItem(cacheKey);
        if (cached) {
          const parsed = JSON.parse(cached);
          setStatus(parsed);
          if (parsed?.connected) {
            setBaseUrl(parsed.baseUrl ?? "");
            setUsername(parsed.username ?? "");
          }
        }
      } catch {
        localStorage.removeItem(cacheKey);
      }

      const result = await service.getStatus();
      if (result) {
        setStatus(result);
        localStorage.setItem(cacheKey, JSON.stringify(result));
        if (result.connected) {
          localStorage.setItem(localStorageConnectedKey, "true");
        } else {
          localStorage.removeItem(localStorageConnectedKey);
        }
        if (result.connected) {
          setBaseUrl(result.baseUrl ?? "");
          setUsername(result.username ?? "");
        }
      }
    } catch (err) {
      console.error(`Failed to load ${displayName} status`, err);
    } finally {
      setCheckingStatus(false);
    }
  };

  useEffect(() => { loadStatus(); }, [slug]);

  useEffect(() => {
    if (status?.connected) {
      service.getWebhookUrl().then(info => { if (info) setWebhookInfo(info); }).catch(() => {});
      service.getDeployments(10).then(data => { if (data) setDeployments(data.deployments); }).catch(() => {});
      service.getRcaSettings().then(data => { if (data) setRcaSettings(data); }).catch(() => {});
    }
  }, [status?.connected, service]);

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ title: "Copy failed", description: "Unable to copy to clipboard", variant: "destructive" });
    }
  };

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      const connectResult = await service.connect({ baseUrl, username, apiToken });
      setStatus(connectResult);
      localStorage.setItem(localStorageConnectedKey, "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: slug }),
        });
      } catch { /* best-effort */ }

      service.getStatus().then(fullStatus => {
        if (fullStatus?.connected) {
          setStatus(fullStatus);
          localStorage.setItem(cacheKey, JSON.stringify(fullStatus));
        }
      }).catch(() => {});

      toast({
        title: `${displayName} Connected`,
        description: `Successfully connected to ${baseUrl || displayName}`,
      });
    } catch (err: unknown) {
      console.error(`${displayName} connection failed`, err);
      toast({ title: "Connection Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setApiToken("");
      setShowToken(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch(`/api/connected-accounts/${slug}`, { method: "DELETE", credentials: "include" });
      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || `Failed to disconnect ${displayName}`);
      }

      setStatus({ connected: false });
      setBaseUrl("");
      setUsername("");
      localStorage.removeItem(cacheKey);
      localStorage.removeItem(localStorageConnectedKey);
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "remove", provider: slug }),
        });
      } catch { /* best-effort */ }

      toast({ title: "Disconnected", description: `${displayName} has been disconnected.` });
    } catch (err: unknown) {
      console.error(`${displayName} disconnect failed`, err);
      toast({ title: "Disconnect Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleRcaToggle = async (enabled: boolean) => {
    setRcaToggleLoading(true);
    const result = await service.updateRcaSettings({ rcaEnabled: enabled });
    if (result) {
      setRcaSettings(result);
    }
    setRcaToggleLoading(false);
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
          <img src={config.logoPath} alt={config.logoAlt} className="h-9 w-9 object-contain rounded-md" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{displayName}</h1>
          <p className="text-muted-foreground text-sm">{config.description}</p>
        </div>
        {isConnected && (
          <Badge className="ml-auto bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 border-green-200 dark:border-green-800 hover:bg-green-100">
            <Check className="h-3 w-3 mr-1" />
            Connected
          </Badge>
        )}
      </div>

      {isConnected ? (
        <ConnectedView
          config={config}
          status={status}
          webhookInfo={webhookInfo}
          deployments={deployments}
          rcaSettings={rcaSettings}
          rcaToggleLoading={rcaToggleLoading}
          onRcaToggle={handleRcaToggle}
          loading={loading}
          copied={copied}
          onDisconnect={handleDisconnect}
          onCopy={copyToClipboard}
        />
      ) : (
        <SetupView
          config={config}
          expandedStep={expandedStep}
          toggleStep={toggleStep}
          baseUrl={baseUrl}
          setBaseUrl={setBaseUrl}
          username={username}
          setUsername={setUsername}
          apiToken={apiToken}
          setApiToken={setApiToken}
          showToken={showToken}
          setShowToken={setShowToken}
          loading={loading}
          onConnect={handleConnect}
        />
      )}
    </div>
  );
}

function ConnectedView({
  config, status, webhookInfo, deployments, rcaSettings, rcaToggleLoading, onRcaToggle, loading, copied, onDisconnect, onCopy,
}: {
  config: CIProviderConfig;
  status: CIProviderStatus | null;
  webhookInfo: CIWebhookInfo | null;
  deployments: CIDeploymentEvent[];
  rcaSettings: CIRcaSettings | null;
  rcaToggleLoading: boolean;
  onRcaToggle: (enabled: boolean) => void;
  loading: boolean;
  copied: boolean;
  onDisconnect: () => void;
  onCopy: (text: string) => void;
}) {
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="pt-6 space-y-6">
          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium truncate">{status?.baseUrl}</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                {status?.username}
                {status?.server?.version ? ` \u00b7 v${status.server.version}` : ""}
                {status?.server?.mode ? ` \u00b7 ${status.server.mode.charAt(0).toUpperCase()}${status.server.mode.slice(1).toLowerCase()}` : ""}
              </p>
            </div>
            {(() => {
              const safeUrl = toSafeExternalUrl(status?.baseUrl);
              return safeUrl ? (
                <a href={safeUrl} target="_blank" rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors shrink-0">
                  Open Dashboard
                  <ExternalLink className="h-3 w-3" />
                </a>
              ) : null;
            })()}
          </div>

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

          <JobHealthBar status={status} />
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-6">
          <div className="flex items-center justify-between">
            <div className="space-y-1 flex-1">
              <h4 className="font-medium flex items-center gap-2">
                <Zap className="h-4 w-4" />
                Automatic RCA on Deployment Failures
              </h4>
              <p className="text-sm text-muted-foreground">
                Automatically trigger root cause analysis when a {config.displayName} build fails or is unstable
              </p>
            </div>
            <Switch
              checked={rcaSettings?.rcaEnabled ?? true}
              onCheckedChange={onRcaToggle}
              disabled={rcaToggleLoading}
              className="ml-4"
            />
          </div>
        </CardContent>
      </Card>

      {webhookInfo && (
        <WebhookCard config={config} webhookInfo={webhookInfo} copied={copied} onCopy={onCopy} />
      )}

      {deployments.length > 0 && (
        <DeploymentsCard config={config} deployments={deployments} />
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

function JobHealthBar({ status }: { status: CIProviderStatus | null }) {
  if (!status?.summary || status.summary.jobCount <= 0) return null;
  const { jobCount, jobHealth } = status.summary;
  const segments: { key: string; count: number; color: string; label: string }[] = [
    { key: "healthy", count: jobHealth.healthy, color: "bg-green-500", label: "Passing" },
    { key: "unstable", count: jobHealth.unstable, color: "bg-yellow-500", label: "Unstable" },
    { key: "failing", count: jobHealth.failing, color: "bg-red-500", label: "Failing" },
    { key: "disabled", count: jobHealth.disabled, color: "bg-muted-foreground/30", label: "Disabled" },
    { key: "other", count: jobHealth.other, color: "bg-muted-foreground/15", label: "Other" },
  ];
  return (
    <>
      <div className="border-t" />
      <div className="space-y-2.5">
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Job Health</p>
        <div className="flex h-2 rounded-full overflow-hidden bg-muted">
          {segments.map(s => s.count > 0 && (
            <div key={s.key} className={s.color} style={{ width: `${(s.count / jobCount) * 100}%` }} />
          ))}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {segments.map(s => s.count > 0 && (
            <span key={s.key} className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${s.color}`} />
              {s.count} {s.label}
            </span>
          ))}
        </div>
      </div>
    </>
  );
}

function WebhookCard({
  config, webhookInfo, copied, onCopy,
}: {
  config: CIProviderConfig;
  webhookInfo: CIWebhookInfo;
  copied: boolean;
  onCopy: (text: string) => void;
}) {
  const iconColor = config.slug === "jenkins" ? "text-orange-600" : "text-violet-600";
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Webhook className={`h-5 w-5 ${iconColor}`} />
          <CardTitle className="text-lg">Send Deployment Events to Aurora</CardTitle>
        </div>
        <CardDescription>
          Add this to your {config.slug === "jenkins" ? "Jenkinsfile" : `${config.displayName} pipeline`} to notify Aurora when builds complete
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="relative">
          <pre className="text-xs bg-muted p-3 rounded-lg whitespace-pre-wrap break-all pr-20">
            <code>{webhookInfo.jenkinsfileCurl}</code>
          </pre>
          <Button size="sm" variant="secondary" className="absolute top-2 right-2 h-8 gap-1.5"
            onClick={() => onCopy(webhookInfo.jenkinsfileCurl)}>
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            {copied ? "Copied!" : "Copy"}
          </Button>
        </div>

        <details className="text-xs group">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground flex items-center gap-1">
            <svg className="h-3 w-3 transition-transform group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
            </svg>
            Alternative snippets (with plugins)
          </summary>
          <div className="mt-3 space-y-3 pl-4 border-l-2 border-muted">
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-medium">With HTTP Request Plugin</span>
                <Button size="sm" variant="ghost" className="h-6 text-xs gap-1" onClick={() => onCopy(webhookInfo.jenkinsfileBasic)}>
                  <Copy className="h-3 w-3" /> Copy
                </Button>
              </div>
              <p className="text-muted-foreground mb-2">
                Better error handling. Requires{" "}
                <a href="https://plugins.jenkins.io/http_request/" target="_blank" rel="noopener noreferrer" className="underline hover:text-foreground">HTTP Request Plugin</a>
              </p>
              <pre className="bg-muted/50 p-2 rounded text-[11px] whitespace-pre-wrap break-all"><code>{webhookInfo.jenkinsfileBasic}</code></pre>
            </div>
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-medium">With Trace Correlation</span>
                <Button size="sm" variant="ghost" className="h-6 text-xs gap-1" onClick={() => onCopy(webhookInfo.jenkinsfileOtel)}>
                  <Copy className="h-3 w-3" /> Copy
                </Button>
              </div>
              <p className="text-muted-foreground mb-2">
                Links builds to application traces. Requires{" "}
                <a href="https://plugins.jenkins.io/http_request/" target="_blank" rel="noopener noreferrer" className="underline hover:text-foreground">HTTP Request</a>
                {" + "}
                <a href="https://plugins.jenkins.io/opentelemetry/" target="_blank" rel="noopener noreferrer" className="underline hover:text-foreground">OpenTelemetry</a>
                {" plugins"}
              </p>
              <pre className="bg-muted/50 p-2 rounded text-[11px] whitespace-pre-wrap break-all"><code>{webhookInfo.jenkinsfileOtel}</code></pre>
            </div>
          </div>
        </details>
      </CardContent>
    </Card>
  );
}

function DeploymentsCard({ config, deployments }: { config: CIProviderConfig; deployments: CIDeploymentEvent[] }) {
  const iconColor = config.slug === "jenkins" ? "text-orange-600" : "text-violet-600";
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Rocket className={`h-5 w-5 ${iconColor}`} />
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
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge variant={dep.result === "SUCCESS" ? "default" : dep.result === "FAILURE" ? "destructive" : "secondary"} className="h-5 text-xs shrink-0">
                      {dep.result}
                    </Badge>
                    <span className="font-medium text-sm">{dep.service}</span>
                    <span className="text-xs text-muted-foreground">&rarr; {dep.environment}</span>
                  </div>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
                    <span className="font-mono">#{dep.buildNumber}</span>
                    {duration && (<><span>&bull;</span><span>{duration}</span></>)}
                    {timeAgo && (<><span>&bull;</span><span>{timeAgo}</span></>)}
                    {dep.deployer && dep.deployer !== "automated" && (<><span>&bull;</span><span>by {dep.deployer}</span></>)}
                  </div>
                </div>
                {(() => {
                  const safeBuildUrl = toSafeExternalUrl(dep.buildUrl);
                  return safeBuildUrl ? (
                    <a href={safeBuildUrl} target="_blank" rel="noopener noreferrer"
                      className="ml-3 p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded transition-colors shrink-0"
                      title={`View in ${config.displayName}`}>
                      <ExternalLink className="h-4 w-4" />
                    </a>
                  ) : null;
                })()}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

function SetupView({
  config, expandedStep, toggleStep, baseUrl, setBaseUrl, username, setUsername,
  apiToken, setApiToken, showToken, setShowToken, loading, onConnect,
}: {
  config: CIProviderConfig;
  expandedStep: number | null;
  toggleStep: (step: number) => void;
  baseUrl: string; setBaseUrl: (v: string) => void;
  username: string; setUsername: (v: string) => void;
  apiToken: string; setApiToken: (v: string) => void;
  showToken: boolean; setShowToken: (v: boolean) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}) {
  const stepColor = config.slug === "jenkins" ? "bg-orange-600" : "bg-violet-600";
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Connect Your {config.displayName} Instance</CardTitle>
          <CardDescription>
            Follow these steps to generate an API token and connect {config.displayName} to Aurora
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Step 1 */}
          <div className="border rounded-lg overflow-hidden">
            <button onClick={() => toggleStep(1)}
              className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors">
              <div className="flex items-center gap-3">
                <div className={`flex h-7 w-7 items-center justify-center rounded-full ${stepColor} text-white text-sm font-bold shrink-0`}>1</div>
                <span className="font-medium">{config.setupStepTitle}</span>
              </div>
              <svg className={`w-5 h-5 text-muted-foreground transition-transform duration-200 ${expandedStep === 1 ? "rotate-180" : ""}`}
                fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {expandedStep === 1 && (
              <div className="px-4 pb-4 space-y-3 text-sm border-t pt-4">
                <div className="space-y-2.5">
                  {config.setupStepInstructions.map((instruction, i) => (
                    <div key={i} className="flex items-start gap-2.5">
                      <span className="text-muted-foreground font-mono text-xs mt-0.5 w-4 text-right shrink-0">{i + 1}.</span>
                      <div>{instruction}</div>
                    </div>
                  ))}
                </div>
                {config.setupStepNote ? (
                  config.setupStepNote
                ) : (
                  <div className="mt-3 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded-lg">
                    <a href={config.docsUrl} target="_blank" rel="noopener noreferrer"
                      className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1">
                      {config.docsLabel}
                      <ExternalLink className="w-3 h-3" />
                    </a>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Step 2 */}
          <div className="border rounded-lg overflow-hidden">
            <button onClick={() => toggleStep(2)}
              className="w-full text-left p-4 flex items-center justify-between hover:bg-muted/50 transition-colors">
              <div className="flex items-center gap-3">
                <div className={`flex h-7 w-7 items-center justify-center rounded-full ${stepColor} text-white text-sm font-bold shrink-0`}>2</div>
                <span className="font-medium">Enter Your Credentials</span>
              </div>
              <svg className={`w-5 h-5 text-muted-foreground transition-transform duration-200 ${expandedStep === 2 ? "rotate-180" : ""}`}
                fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {expandedStep === 2 && (
              <div className="px-4 pb-4 border-t pt-4">
                <form onSubmit={onConnect} className="space-y-5">
                  <div className="grid gap-1.5">
                    <Label htmlFor={`${config.slug}-url`} className="text-sm font-medium">{config.displayName} URL</Label>
                    <Input id={`${config.slug}-url`} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                      placeholder={config.urlPlaceholder} required disabled={loading} className="h-10" />
                    <p className="text-xs text-muted-foreground">{config.urlHelpText}</p>
                  </div>
                  <div className="grid gap-1.5">
                    <Label htmlFor={`${config.slug}-username`} className="text-sm font-medium">Username</Label>
                    <Input id={`${config.slug}-username`} value={username} onChange={(e) => setUsername(e.target.value)}
                      placeholder={config.usernamePlaceholder} required disabled={loading} className="h-10" />
                  </div>
                  <div className="grid gap-1.5">
                    <Label htmlFor={`${config.slug}-token`} className="text-sm font-medium">API Token</Label>
                    <div className="relative">
                      <Input id={`${config.slug}-token`} type={showToken ? "text" : "password"} value={apiToken}
                        onChange={(e) => setApiToken(e.target.value)} placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                        required disabled={loading} className="h-10 pr-10" />
                      <button type="button" onClick={() => setShowToken(!showToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                        aria-label={showToken ? "Hide token" : "Show token"}>
                        {showToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    </div>
                  </div>
                  <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
                    <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
                    <div className="space-y-1">
                      <p className="font-medium">Secure &amp; Read-Only</p>
                      <p className="text-muted-foreground">
                        Your credentials are encrypted and stored in Vault. Aurora only reads job and build data &mdash; it cannot trigger builds or modify configuration.
                      </p>
                    </div>
                  </div>
                  <Button type="submit" disabled={loading || !baseUrl || !username || !apiToken} className="w-full h-10">
                    {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : `Connect ${config.displayName}`}
                  </Button>
                </form>
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

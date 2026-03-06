"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import { spinnakerService } from "@/lib/services/spinnaker";
import type {
  SpinnakerStatus,
  SpinnakerWebhookInfo,
  SpinnakerDeploymentEvent,
  SpinnakerRcaSettings,
} from "@/lib/services/spinnaker";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Check, ChevronLeft, Copy, ExternalLink, Eye, EyeOff,
  Loader2, Rocket, ShieldCheck, Upload, Webhook, Zap,
} from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { formatTimeAgo } from "@/lib/utils/time-format";

const CACHE_KEY = "spinnaker_connection_status";
const LOCAL_STORAGE_KEY = "isSpinnakerConnected";

const toSafeExternalUrl = (value?: string): string | null => {
  if (!value) return null;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:" ? u.toString() : null;
  } catch {
    return null;
  }
};

export default function SpinnakerAuthPage() {
  const router = useRouter();
  const { toast } = useToast();

  // Form state
  const [baseUrl, setBaseUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [certContent, setCertContent] = useState("");
  const [keyContent, setKeyContent] = useState("");
  const [caContent, setCaContent] = useState("");
  const [certFileName, setCertFileName] = useState("");
  const [keyFileName, setKeyFileName] = useState("");
  const [caFileName, setCaFileName] = useState("");
  const [authTab, setAuthTab] = useState("token");

  // Connection state
  const [status, setStatus] = useState<SpinnakerStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [webhookInfo, setWebhookInfo] = useState<SpinnakerWebhookInfo | null>(null);
  const [deployments, setDeployments] = useState<SpinnakerDeploymentEvent[]>([]);
  const [rcaSettings, setRcaSettings] = useState<SpinnakerRcaSettings | null>(null);
  const [rcaToggleLoading, setRcaToggleLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const loadStatus = async () => {
    setCheckingStatus(true);
    try {
      try {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          const parsed = JSON.parse(cached);
          setStatus(parsed);
          if (parsed?.connected) {
            setBaseUrl(parsed.baseUrl ?? "");
          }
        }
      } catch {
        localStorage.removeItem(CACHE_KEY);
      }

      const result = await spinnakerService.getStatus();
      if (result) {
        setStatus(result);
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
        if (result.connected) {
          localStorage.setItem(LOCAL_STORAGE_KEY, "true");
          setBaseUrl(result.baseUrl ?? "");
        } else {
          localStorage.removeItem(LOCAL_STORAGE_KEY);
        }
      }
    } catch (err) {
      console.error("Failed to load Spinnaker status", err);
    } finally {
      setCheckingStatus(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  useEffect(() => {
    if (status?.connected) {
      spinnakerService.getWebhookUrl().then(info => { if (info) setWebhookInfo(info); }).catch(() => {});
      spinnakerService.getDeployments(10).then(data => { if (data) setDeployments(data.deployments); }).catch(() => {});
      spinnakerService.getRcaSettings().then(data => { if (data) setRcaSettings(data); }).catch(() => {});
    }
  }, [status?.connected]);

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ title: "Copy failed", description: "Unable to copy to clipboard", variant: "destructive" });
    }
  };

  const readFile = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsText(file);
    });
  };

  const handleFileUpload = async (
    e: React.ChangeEvent<HTMLInputElement>,
    setContent: (v: string) => void,
    setFileName: (v: string) => void,
  ) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const content = await readFile(file);
      setContent(content);
      setFileName(file.name);
    } catch {
      toast({ title: "File read error", description: "Failed to read the selected file", variant: "destructive" });
    }
  };

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    try {
      let payload: Record<string, string>;
      if (authTab === "token") {
        payload = {
          baseUrl,
          username,
          password,
          authType: "basic",
        };
      } else {
        payload = {
          baseUrl,
          authType: "x509",
          certPem: certContent,
          keyPem: keyContent,
          ...(caContent ? { caBundlePem: caContent } : {}),
        };
      }

      const connectResult = await spinnakerService.connect(payload);
      setStatus(connectResult);
      localStorage.setItem(LOCAL_STORAGE_KEY, "true");
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", provider: "spinnaker" }),
        });
      } catch { /* best-effort */ }

      toast({
        title: "Spinnaker Connected",
        description: `Successfully connected to ${baseUrl || "Spinnaker"}`,
      });
    } catch (err: unknown) {
      console.error("Spinnaker connection failed", err);
      toast({ title: "Connection Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
      setPassword("");
      setShowPassword(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/spinnaker", { method: "DELETE", credentials: "include" });
      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Spinnaker");
      }

      setStatus({ connected: false });
      setBaseUrl("");
      setUsername("");
      setPassword("");
      setCertContent("");
      setKeyContent("");
      setCaContent("");
      setCertFileName("");
      setKeyFileName("");
      setCaFileName("");
      localStorage.removeItem(CACHE_KEY);
      localStorage.removeItem(LOCAL_STORAGE_KEY);
      window.dispatchEvent(new CustomEvent("providerStateChanged"));

      try {
        await fetch("/api/provider-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "remove", provider: "spinnaker" }),
        });
      } catch { /* best-effort */ }

      toast({ title: "Disconnected", description: "Spinnaker has been disconnected." });
    } catch (err: unknown) {
      console.error("Spinnaker disconnect failed", err);
      toast({ title: "Disconnect Failed", description: getUserFriendlyError(err), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleRcaToggle = async (enabled: boolean) => {
    setRcaToggleLoading(true);
    try {
      const result = await spinnakerService.updateRcaSettings({ rcaEnabled: enabled });
      if (result) {
        setRcaSettings(result);
      }
    } catch (err) {
      console.error("Failed to toggle RCA setting", err);
      toast({ title: "Update Failed", description: "Failed to update RCA settings", variant: "destructive" });
    } finally {
      setRcaToggleLoading(false);
    }
  };

  const isConnected = Boolean(status?.connected);

  const isTokenFormValid = baseUrl && username && password;
  const isX509FormValid = baseUrl && certContent && keyContent;

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
          <img src="/spinnaker.svg" alt="Spinnaker" className="h-9 w-9 object-contain rounded-md" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Spinnaker</h1>
          <p className="text-muted-foreground text-sm">
            Continuous delivery platform for deployment pipeline visibility and incident correlation
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
          {/* Connection Info Card */}
          <Card>
            <CardContent className="pt-6 space-y-6">
              <div className="flex items-center justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate">{status?.baseUrl}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Auth: {status?.authType || "Token/Basic"}
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

              <div className="border-t" />
              <div className="grid grid-cols-2 gap-4">
                <div className="text-center">
                  <p className="text-2xl font-semibold tabular-nums">{status?.applications ?? 0}</p>
                  <p className="text-[11px] text-muted-foreground mt-0.5">Applications</p>
                </div>
                <div className="text-center">
                  <p className="text-2xl font-semibold tabular-nums">{status?.cloudAccounts?.length ?? 0}</p>
                  <p className="text-[11px] text-muted-foreground mt-0.5">Cloud Accounts</p>
                </div>
              </div>

              {status?.cloudAccounts && status.cloudAccounts.length > 0 && (
                <>
                  <div className="border-t" />
                  <div className="space-y-2">
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Cloud Accounts</p>
                    <div className="flex flex-wrap gap-1.5">
                      {status.cloudAccounts.map((account) => (
                        <Badge key={account} variant="secondary" className="text-xs">
                          {account}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          {/* RCA Toggle Card */}
          <Card>
            <CardContent className="pt-6">
              <div className="flex items-center justify-between">
                <div className="space-y-1 flex-1">
                  <h4 className="font-medium flex items-center gap-2">
                    <Zap className="h-4 w-4" />
                    Automatic RCA on Deployment Failures
                  </h4>
                  <p className="text-sm text-muted-foreground">
                    Automatically trigger root cause analysis when a Spinnaker pipeline fails
                  </p>
                </div>
                <Switch
                  checked={rcaSettings?.rcaEnabled ?? true}
                  onCheckedChange={handleRcaToggle}
                  disabled={rcaToggleLoading}
                  className="ml-4"
                />
              </div>
            </CardContent>
          </Card>

          {/* Webhook Card */}
          {webhookInfo && (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-center gap-2">
                  <Webhook className="h-5 w-5 text-teal-600" />
                  <CardTitle className="text-lg">Send Deployment Events to Aurora</CardTitle>
                </div>
                <CardDescription>
                  Configure Spinnaker Echo to send pipeline events to Aurora for deployment tracking
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <p className="text-xs font-medium text-muted-foreground mb-1.5">Webhook URL</p>
                  <div className="relative">
                    <pre className="text-xs bg-muted p-3 rounded-lg whitespace-pre-wrap break-all pr-20">
                      <code>{webhookInfo.webhookUrl}</code>
                    </pre>
                    <Button size="sm" variant="secondary" className="absolute top-2 right-2 h-8 gap-1.5"
                      onClick={() => copyToClipboard(webhookInfo.webhookUrl)}>
                      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                      {copied ? "Copied!" : "Copy"}
                    </Button>
                  </div>
                </div>

                {webhookInfo.echoConfig && (
                  <div>
                    <p className="text-xs font-medium text-muted-foreground mb-1.5">Echo Configuration</p>
                    <pre className="text-xs bg-muted p-3 rounded-lg whitespace-pre-wrap break-all">
                      <code>{webhookInfo.echoConfig}</code>
                    </pre>
                  </div>
                )}

                {webhookInfo.instructions && webhookInfo.instructions.length > 0 && (
                  <div className="space-y-1.5 text-xs text-muted-foreground">
                    <p className="font-medium text-foreground">Setup Instructions:</p>
                    {webhookInfo.instructions.map((instruction, i) => (
                      <div key={i} className="flex items-start gap-2">
                        <span className="font-mono w-4 text-right shrink-0">{i + 1}.</span>
                        <span>{instruction}</span>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          )}

          {/* Recent Deployments Card */}
          {deployments.length > 0 && (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-center gap-2">
                  <Rocket className="h-5 w-5 text-teal-600" />
                  <CardTitle className="text-lg">Recent Deployments</CardTitle>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {deployments.map((dep) => {
                    const timeAgo = dep.receivedAt ? formatTimeAgo(new Date(dep.receivedAt)) : null;
                    return (
                      <div key={dep.id} className="flex items-start justify-between p-3 rounded-lg border bg-card hover:bg-muted/50 transition-colors">
                        <div className="flex-1 min-w-0 space-y-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <Badge
                              variant={
                                dep.status === "SUCCEEDED" ? "default" :
                                dep.status === "TERMINAL" || dep.status === "FAILED_CONTINUE" ? "destructive" :
                                "secondary"
                              }
                              className="h-5 text-xs shrink-0"
                            >
                              {dep.status}
                            </Badge>
                            <span className="font-medium text-sm">{dep.application}</span>
                          </div>
                          <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
                            <span>{dep.pipelineName}</span>
                            {dep.triggerType && (
                              <>
                                <span>&bull;</span>
                                <span>{dep.triggerType}</span>
                              </>
                            )}
                            {timeAgo && (
                              <>
                                <span>&bull;</span>
                                <span>{timeAgo}</span>
                              </>
                            )}
                            {dep.triggerUser && dep.triggerUser !== "anonymous" && (
                              <>
                                <span>&bull;</span>
                                <span>by {dep.triggerUser}</span>
                              </>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          )}

          {/* Disconnect */}
          <div className="flex items-center justify-between px-1">
            <p className="text-xs text-muted-foreground">Remove stored credentials and disconnect</p>
            <Button variant="ghost" size="sm" onClick={handleDisconnect} disabled={loading}
              className="text-red-500 hover:text-red-600 hover:bg-red-500/10 dark:text-red-400 dark:hover:text-red-300 h-8 text-xs">
              {loading ? <Loader2 className="h-3 w-3 animate-spin mr-1.5" /> : null}
              {loading ? "Disconnecting..." : "Disconnect"}
            </Button>
          </div>
        </div>
      ) : (
        /* Setup View */
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Connect Your Spinnaker Instance</CardTitle>
              <CardDescription>
                Spinnaker supports multiple authentication methods. Choose the one that matches your deployment.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Tabs value={authTab} onValueChange={setAuthTab} className="w-full">
                <TabsList className="grid w-full grid-cols-2">
                  <TabsTrigger value="token">Token / Basic Auth</TabsTrigger>
                  <TabsTrigger value="x509">X.509 Certificate</TabsTrigger>
                </TabsList>

                {/* Token / Basic Auth Tab */}
                <TabsContent value="token" className="mt-6">
                  <form onSubmit={handleConnect} className="space-y-5">
                    <div className="grid gap-1.5">
                      <Label htmlFor="spinnaker-url" className="text-sm font-medium">Spinnaker Gate URL</Label>
                      <Input
                        id="spinnaker-url"
                        value={baseUrl}
                        onChange={(e) => setBaseUrl(e.target.value)}
                        placeholder="https://spinnaker-gate.example.com"
                        required
                        disabled={loading}
                        className="h-10"
                      />
                      <p className="text-xs text-muted-foreground">
                        Full URL to your Spinnaker Gate API (e.g. https://gate.spinnaker.example.com)
                      </p>
                    </div>

                    <div className="grid gap-1.5">
                      <Label htmlFor="spinnaker-username" className="text-sm font-medium">Username</Label>
                      <Input
                        id="spinnaker-username"
                        value={username}
                        onChange={(e) => setUsername(e.target.value)}
                        placeholder="your-username"
                        required
                        disabled={loading}
                        className="h-10"
                      />
                    </div>

                    <div className="grid gap-1.5">
                      <Label htmlFor="spinnaker-password" className="text-sm font-medium">Password / API Token</Label>
                      <div className="relative">
                        <Input
                          id="spinnaker-password"
                          type={showPassword ? "text" : "password"}
                          value={password}
                          onChange={(e) => setPassword(e.target.value)}
                          placeholder="Enter password or API token"
                          required
                          disabled={loading}
                          className="h-10 pr-10"
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword(!showPassword)}
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                          aria-label={showPassword ? "Hide password" : "Show password"}
                        >
                          {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                        </button>
                      </div>
                    </div>

                    <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
                      <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
                      <div className="space-y-1">
                        <p className="font-medium">Secure Connection</p>
                        <p className="text-muted-foreground">
                          Your credentials are encrypted and stored in Vault. Aurora monitors applications and pipelines, and can trigger pipelines (e.g., rollback) with your confirmation.
                        </p>
                      </div>
                    </div>

                    <Button type="submit" disabled={loading || !isTokenFormValid} className="w-full h-10">
                      {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : "Connect Spinnaker"}
                    </Button>
                  </form>
                </TabsContent>

                {/* X.509 Certificate Tab */}
                <TabsContent value="x509" className="mt-6">
                  <form onSubmit={handleConnect} className="space-y-5">
                    <div className="grid gap-1.5">
                      <Label htmlFor="spinnaker-x509-url" className="text-sm font-medium">Spinnaker Gate URL</Label>
                      <Input
                        id="spinnaker-x509-url"
                        value={baseUrl}
                        onChange={(e) => setBaseUrl(e.target.value)}
                        placeholder="https://spinnaker-gate-x509.example.com"
                        required
                        disabled={loading}
                        className="h-10"
                      />
                      <p className="text-xs text-muted-foreground">
                        Full URL to your Spinnaker Gate X.509 endpoint (often a separate port, e.g. :8085)
                      </p>
                    </div>

                    <div className="grid gap-1.5">
                      <Label className="text-sm font-medium">Client Certificate (PEM)</Label>
                      <div className="flex items-center gap-3">
                        <Label
                          htmlFor="cert-upload"
                          className="flex items-center gap-2 px-4 py-2 rounded-md border cursor-pointer hover:bg-muted/50 transition-colors text-sm"
                        >
                          <Upload className="h-4 w-4" />
                          {certFileName || "Choose certificate file"}
                        </Label>
                        <input
                          id="cert-upload"
                          type="file"
                          accept=".pem,.crt,.cert"
                          className="hidden"
                          onChange={(e) => handleFileUpload(e, setCertContent, setCertFileName)}
                          disabled={loading}
                        />
                        {certContent && (
                          <Badge variant="secondary" className="text-xs">
                            <Check className="h-3 w-3 mr-1" /> Loaded
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">PEM-encoded client certificate (.pem, .crt)</p>
                    </div>

                    <div className="grid gap-1.5">
                      <Label className="text-sm font-medium">Client Private Key (PEM)</Label>
                      <div className="flex items-center gap-3">
                        <Label
                          htmlFor="key-upload"
                          className="flex items-center gap-2 px-4 py-2 rounded-md border cursor-pointer hover:bg-muted/50 transition-colors text-sm"
                        >
                          <Upload className="h-4 w-4" />
                          {keyFileName || "Choose key file"}
                        </Label>
                        <input
                          id="key-upload"
                          type="file"
                          accept=".pem,.key"
                          className="hidden"
                          onChange={(e) => handleFileUpload(e, setKeyContent, setKeyFileName)}
                          disabled={loading}
                        />
                        {keyContent && (
                          <Badge variant="secondary" className="text-xs">
                            <Check className="h-3 w-3 mr-1" /> Loaded
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">PEM-encoded private key (.pem, .key)</p>
                    </div>

                    <div className="grid gap-1.5">
                      <Label className="text-sm font-medium">
                        CA Bundle (PEM) <span className="text-muted-foreground font-normal">- optional</span>
                      </Label>
                      <div className="flex items-center gap-3">
                        <Label
                          htmlFor="ca-upload"
                          className="flex items-center gap-2 px-4 py-2 rounded-md border cursor-pointer hover:bg-muted/50 transition-colors text-sm"
                        >
                          <Upload className="h-4 w-4" />
                          {caFileName || "Choose CA bundle file"}
                        </Label>
                        <input
                          id="ca-upload"
                          type="file"
                          accept=".pem,.crt,.cert"
                          className="hidden"
                          onChange={(e) => handleFileUpload(e, setCaContent, setCaFileName)}
                          disabled={loading}
                        />
                        {caContent && (
                          <Badge variant="secondary" className="text-xs">
                            <Check className="h-3 w-3 mr-1" /> Loaded
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        CA certificate bundle for verifying the server (only needed for self-signed or private CAs)
                      </p>
                    </div>

                    <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
                      <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
                      <div className="space-y-1">
                        <p className="font-medium">Secure Connection</p>
                        <p className="text-muted-foreground">
                          Certificate and key data are encrypted and stored in Vault. Aurora monitors applications and pipelines, and can trigger pipelines (e.g., rollback) with your confirmation.
                        </p>
                      </div>
                    </div>

                    <Button type="submit" disabled={loading || !isX509FormValid} className="w-full h-10">
                      {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : "Connect Spinnaker"}
                    </Button>
                  </form>
                </TabsContent>
              </Tabs>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

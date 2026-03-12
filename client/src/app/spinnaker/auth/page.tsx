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
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Check, ChevronLeft, Loader2 } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";

import { TokenAuthForm } from "./components/TokenAuthForm";
import { X509AuthForm } from "./components/X509AuthForm";
import { ConnectionInfo } from "./components/ConnectionInfo";
import { RcaToggle } from "./components/RcaToggle";
import { WebhookPanel } from "./components/WebhookPanel";
import { DeploymentsList } from "./components/DeploymentsList";

const CACHE_KEY = "spinnaker_connection_status";
const LOCAL_STORAGE_KEY = "isSpinnakerConnected";

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
          <ConnectionInfo status={status!} />
          <RcaToggle rcaSettings={rcaSettings} loading={rcaToggleLoading} onToggle={handleRcaToggle} />
          {webhookInfo && <WebhookPanel webhookInfo={webhookInfo} />}
          <DeploymentsList deployments={deployments} />

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

                <TabsContent value="token" className="mt-6">
                  <TokenAuthForm
                    baseUrl={baseUrl} setBaseUrl={setBaseUrl}
                    username={username} setUsername={setUsername}
                    password={password} setPassword={setPassword}
                    showPassword={showPassword} setShowPassword={setShowPassword}
                    loading={loading} onSubmit={handleConnect}
                  />
                </TabsContent>

                <TabsContent value="x509" className="mt-6">
                  <X509AuthForm
                    baseUrl={baseUrl} setBaseUrl={setBaseUrl}
                    certContent={certContent} keyContent={keyContent} caContent={caContent}
                    certFileName={certFileName} keyFileName={keyFileName} caFileName={caFileName}
                    loading={loading} onSubmit={handleConnect} onFileUpload={handleFileUpload}
                    setCertContent={setCertContent} setKeyContent={setKeyContent} setCaContent={setCaContent}
                    setCertFileName={setCertFileName} setKeyFileName={setKeyFileName} setCaFileName={setCaFileName}
                  />
                </TabsContent>
              </Tabs>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

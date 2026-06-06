"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Check, Copy, Loader2, AlertCircle, Upload, FileText, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/hooks/use-toast";
import { copyToClipboard } from "@/lib/utils";
import { KUBECTL_AGENT } from "@/lib/kubectl-constants";
import { getEnv } from '@/lib/env';
import { Toaster } from "@/components/ui/toaster";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const wsUrl = getEnv('NEXT_PUBLIC_WEBSOCKET_URL') || '';
const backendUrl = getEnv('NEXT_PUBLIC_BACKEND_URL') || '';
const hasRequiredEndpoints = Boolean(wsUrl && backendUrl);

const getHelmInstallCommand = (token: string) => hasRequiredEndpoints
  ? String.raw`helm install ${KUBECTL_AGENT.RELEASE_NAME} ${KUBECTL_AGENT.CHART_OCI_URL} \
  --version ${KUBECTL_AGENT.CHART_VERSION} \
  --create-namespace \
  --namespace ${KUBECTL_AGENT.DEFAULT_NAMESPACE} \
  --set aurora.agentToken="${token}" \
  --set aurora.backendUrl="${backendUrl}" \
  --set aurora.wsEndpoint="${wsUrl}"`
  : '# Error: NEXT_PUBLIC_BACKEND_URL or NEXT_PUBLIC_WEBSOCKET_URL is not configured';

interface PendingFile {
  filename: string;
  content: string;
}

function CommandRow({ label, command, copyKey, copied, onCopy }: Readonly<{
  label: string; command: string; copyKey: string;
  copied: string | null; onCopy: (cmd: string, key: string) => void;
}>) {
  return (
    <div className="space-y-2">
      <Label className="text-sm text-zinc-300">{label}</Label>
      <div className="relative">
        <pre className="overflow-auto rounded-lg bg-zinc-900 border border-zinc-800 p-2 pr-12 text-xs leading-relaxed font-mono text-zinc-100">
          {command}
        </pre>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onCopy(command, copyKey)}
          className={`absolute right-1 text-zinc-400 hover:text-zinc-100 hover:bg-transparent ${command.includes('\n') ? 'top-1' : 'top-1/2 -translate-y-1/2'}`}
        >
          {copied === copyKey ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  );
}

export default function KubectlAuthPage() {
  const { toast } = useToast();
  const router = useRouter();
  const [copied, setCopied] = useState<string | null>(null);
  const [generatingToken, setGeneratingToken] = useState(false);
  const [generatedToken, setGeneratedToken] = useState<string | null>(null);
  const [clusterName, setClusterName] = useState("");
  const [clusterMetadata, setClusterMetadata] = useState("");
  const [showTokenForm, setShowTokenForm] = useState(false);
  const [agentStatus, setAgentStatus] = useState<'checking' | 'connected' | 'not_connected'>('checking');

  // Kubeconfig upload state
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    if (!generatedToken) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/kubectl/status?token=${encodeURIComponent(generatedToken)}`);
        const data = await res.json();
        if (data.connected) {
          setAgentStatus('connected');
          localStorage.setItem(KUBECTL_AGENT.STORAGE_KEY, 'true');
          globalThis.dispatchEvent(new CustomEvent('providerStateChanged'));
        }
      } catch (error) {
        console.error('Error checking kubectl status:', error);
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [generatedToken]);

  const copyCommand = async (text: string, key: string) => {
    try {
      await copyToClipboard(text);
      setCopied(key);
      setTimeout(() => setCopied(null), 2000);
    } catch (error) {
      console.error("Failed to copy", error);
    }
  };

  const generateToken = async () => {
    if (!clusterName.trim()) {
      toast({ title: "Cluster name required", description: "Please provide a name for your cluster", variant: "destructive" });
      return;
    }
    setGeneratingToken(true);
    try {
      const response = await fetch('/api/kubectl/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cluster_name: clusterName.trim(), notes: clusterMetadata.trim() }),
      });
      if (!response.ok) throw new Error('Failed to generate token');
      const data = await response.json();
      setGeneratedToken(data.token);
      setShowTokenForm(false);
      toast({ title: "Token generated", description: "Copy this token now - it won't be shown again!" });
    } catch (error) {
      console.error('Error generating token:', error);
      toast({ title: "Error", description: "Failed to generate token. Please try again.", variant: "destructive" });
    } finally {
      setGeneratingToken(false);
    }
  };

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    setUploadError(null);

    for (const file of Array.from(files)) {
      if (file.size > 500 * 1024) {
        setUploadError(`${file.name} exceeds 500KB limit`);
        continue;
      }
      const content = await file.text();
      const lines = content.split('\n');
      const hasApiVersion = lines.some(l => l.startsWith('apiVersion:'));
      const hasClusters = lines.some(l => l.startsWith('clusters:'));
      const hasContexts = lines.some(l => l.startsWith('contexts:'));
      if (!hasApiVersion || !hasClusters || !hasContexts) {
        setUploadError(`${file.name} does not appear to be a valid kubeconfig file`);
        continue;
      }
      setPendingFiles(prev => [...prev.filter(f => f.filename !== file.name), { filename: file.name, content }]);
    }
    e.target.value = '';
  };

  const removePendingFile = (filename: string) => {
    setPendingFiles(prev => prev.filter(f => f.filename !== filename));
  };

  const uploadKubeconfigs = async () => {
    if (pendingFiles.length === 0) return;
    setUploading(true);
    try {
      const res = await fetch('/api/kubeconfig/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kubeconfigs: pendingFiles }),
      });
      const data = await res.json();
      if (!res.ok) {
        const errorMsg = data.errors?.[0]?.error || data.error || 'Upload failed';
        toast({ title: "Upload failed", description: errorMsg, variant: "destructive" });
        return;
      }
      const registered = data.registered || [];
      const errors = data.errors || [];
      if (registered.length > 0) {
        localStorage.setItem(KUBECTL_AGENT.STORAGE_KEY, 'true');
        globalThis.dispatchEvent(new CustomEvent('providerStateChanged'));
        toast({ title: "Kubeconfigs uploaded", description: `${registered.length} cluster(s) registered successfully` });
        setPendingFiles([]);
      }
      if (errors.length > 0) {
        errors.forEach((err: { filename: string; error: string }) => {
          toast({ title: `Error: ${err.filename}`, description: err.error, variant: "destructive" });
        });
      }
    } catch (error) {
      console.error('Kubeconfig upload error:', error);
      toast({ title: "Error", description: "Failed to upload kubeconfigs", variant: "destructive" });
    } finally {
      setUploading(false);
    }
  };


  return (
    <ConnectorAuthGuard connectorName="Kubectl">
      <div className="min-h-screen bg-black">
        <div className="container mx-auto py-12 px-4 max-w-4xl">
          <div className="flex justify-end mb-6">
            <Button
              variant="outline"
              size="sm"
              onClick={() => router.push('/kubectl/manage')}
              className="border-zinc-700 hover:bg-zinc-900"
            >
              Manage Clusters
            </Button>
          </div>

          <div className="mb-8 flex flex-col items-center text-center">
            <img src="/kubernetes_text.png" alt="Kubernetes" className="h-32 w-auto mb-4" />
            <p className="text-sm text-zinc-400 max-w-xl mb-1">
              Connect your Kubernetes clusters to Aurora for intelligent incident investigation.
            </p>
            <p className="text-xs text-zinc-500">
              All connections are secure and encrypted
            </p>
          </div>

          <Tabs defaultValue="agent" className="w-full">
            <TabsList className="w-full bg-zinc-900 border border-zinc-800 h-auto p-1">
              <TabsTrigger value="agent" className="data-[state=active]:bg-zinc-800 flex-1 py-2.5">
                Deploy Agent
                <Badge variant="secondary" className="ml-2 text-[10px] px-1.5 py-0 bg-zinc-700 text-zinc-300">
                  Recommended
                </Badge>
              </TabsTrigger>
              <TabsTrigger value="kubeconfig" className="data-[state=active]:bg-zinc-800 flex-1 py-2.5">
                Upload Kubeconfig
              </TabsTrigger>
            </TabsList>

            {/* === Agent Tab === */}
            <TabsContent value="agent" className="mt-6 space-y-6">
              <Card className="bg-zinc-950 border-zinc-800">
                <CardHeader className="pb-4">
                  <CardTitle className="text-white text-lg tracking-tight">1. Generate Agent Token</CardTitle>
                  <CardDescription className="text-zinc-400 text-sm">
                    Create an authentication token for your kubernetes agent.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  {!generatedToken && !showTokenForm && (
                    <Button onClick={() => setShowTokenForm(true)} className="bg-white text-black hover:bg-zinc-200">
                      Generate New Token
                    </Button>
                  )}
                  {showTokenForm && !generatedToken && (
                    <div className="space-y-4">
                      <div className="space-y-2">
                        <Label htmlFor="clusterName" className="text-white text-sm">
                          Cluster Name <span className="text-red-400">*</span>
                        </Label>
                        <Input
                          id="clusterName"
                          placeholder="e.g., production-k8s-cluster"
                          value={clusterName}
                          onChange={(e) => setClusterName(e.target.value)}
                          className="bg-zinc-900 border-zinc-800 text-white placeholder:text-zinc-500 focus-visible:ring-zinc-700"
                        />
                        <p className="text-xs text-zinc-500 mt-1.5">
                          Use the same cluster name as in your alerting system for seamless incident correlation
                        </p>
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="clusterMetadata" className="text-white text-sm">
                          Cluster Context <span className="text-zinc-500 text-xs">(Optional)</span>
                        </Label>
                        <textarea
                          id="clusterMetadata"
                          placeholder="e.g., Production environment, US-East region"
                          value={clusterMetadata}
                          onChange={(e) => setClusterMetadata(e.target.value)}
                          className="w-full h-20 px-3 py-2 rounded-md bg-zinc-900 border border-zinc-800 text-white placeholder:text-zinc-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-700 text-sm resize-none"
                        />
                      </div>
                      <div className="flex gap-3">
                        <Button onClick={generateToken} disabled={generatingToken || !clusterName.trim()} className="bg-white text-black hover:bg-zinc-200">
                          {generatingToken ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" />Generating...</> : "Generate Token"}
                        </Button>
                        <Button variant="outline" onClick={() => setShowTokenForm(false)} className="border-zinc-700 text-zinc-300 hover:bg-zinc-800">
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                  {generatedToken && (
                    <div className="space-y-4">
                      <Alert className="bg-zinc-900 border-zinc-700">
                        <AlertDescription className="text-zinc-300 text-sm">
                          Save this token now - it will only be shown once!
                        </AlertDescription>
                      </Alert>
                      <div className="space-y-2">
                        <Label className="text-white text-sm">Your Agent Token</Label>
                        <div className="relative">
                          <Input value={generatedToken} readOnly className="bg-zinc-900 border-zinc-800 text-white pr-12 font-mono text-xs" />
                          <Button variant="ghost" size="sm" onClick={() => copyCommand(generatedToken, "token")} className="absolute top-1/2 -translate-y-1/2 right-1 text-zinc-400 hover:text-zinc-100 hover:bg-transparent">
                            {copied === "token" ? <Check className="h-4 w-4 text-green-400" /> : <Copy className="h-4 w-4" />}
                          </Button>
                        </div>
                      </div>
                      <Button variant="outline" size="sm" onClick={() => { setGeneratedToken(null); setClusterName(""); }} className="border-zinc-700 text-zinc-300 hover:bg-zinc-800">
                        Generate Another Token
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>

              <Card className="bg-zinc-950 border-zinc-800">
                <CardHeader className="pb-4">
                  <CardTitle className="text-white text-lg tracking-tight">2. Set your kubectl context</CardTitle>
                  <CardDescription className="text-zinc-400 text-sm">
                    Verify you're connected to the correct cluster.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <CommandRow label="Check current context" command="kubectl config current-context" copyKey="context-check" copied={copied} onCopy={copyCommand} />
                  <CommandRow label="Switch context (if needed)" command="kubectl config use-context <context-name>" copyKey="context-switch" copied={copied} onCopy={copyCommand} />
                </CardContent>
              </Card>

              <Card className="bg-zinc-950 border-zinc-800">
                <CardHeader className="pb-4">
                  <CardTitle className="text-white text-lg tracking-tight">3. Install with Helm</CardTitle>
                  <CardDescription className="text-zinc-400 text-sm">
                    Deploy the agent with read-only RBAC into your cluster.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  {!generatedToken && (
                    <Alert className="bg-zinc-900 border-zinc-800">
                      <AlertCircle className="h-4 w-4 text-yellow-500" />
                      <AlertDescription className="text-sm text-zinc-300">
                        Generate a token in Step 1 first.
                      </AlertDescription>
                    </Alert>
                  )}
                  <CommandRow label="Install with Helm" command={getHelmInstallCommand(generatedToken || "<YOUR_TOKEN>")} copyKey="helm" copied={copied} onCopy={copyCommand} />
                </CardContent>
              </Card>

              <Card className="bg-zinc-950 border-zinc-800">
                <CardHeader className="pb-4">
                  <CardTitle className="text-white text-lg tracking-tight">4. Verify connection</CardTitle>
                  <CardDescription className="text-zinc-400 text-sm">
                    Aurora will detect the agent automatically once deployed.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  {agentStatus === 'connected' ? (
                    <div className="flex items-center gap-3 p-4 bg-green-950/30 border border-green-900/50 rounded-lg">
                      <Check className="h-5 w-5 text-green-400" />
                      <div>
                        <p className="text-green-400 text-sm">Agent Connected</p>
                        <p className="text-zinc-400 text-xs mt-0.5">Your kubernetes agent is connected and ready</p>
                      </div>
                    </div>
                  ) : (
                    <div className="flex items-center gap-3 p-4 bg-zinc-900 border border-zinc-800 rounded-lg">
                      <Loader2 className="h-5 w-5 text-zinc-400 animate-spin" />
                      <div>
                        <p className="text-zinc-300 text-sm">Waiting for agent...</p>
                        <p className="text-zinc-500 text-xs mt-0.5">Deploy the agent using Step 3</p>
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            {/* === Kubeconfig Upload Tab === */}
            <TabsContent value="kubeconfig" className="mt-6 space-y-6">
              <Card className="bg-zinc-950 border-zinc-800">
                <CardHeader className="pb-4">
                  <CardTitle className="text-white text-lg tracking-tight">Upload Kubeconfig Files</CardTitle>
                  <CardDescription className="text-zinc-400 text-sm">
                    Use this when you cannot deploy the Aurora agent inside your cluster (e.g., restricted RBAC, air-gapped environments).
                    Aurora will execute kubectl commands directly using these credentials.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="space-y-3">
                    <input
                      id="kubeconfig-upload"
                      type="file"
                      accept=".yaml,.yml"
                      multiple
                      className="sr-only"
                      onChange={handleFileSelect}
                    />
                    <label
                      htmlFor="kubeconfig-upload"
                      className="flex items-center justify-center w-full py-8 border border-dashed border-zinc-700 rounded-md cursor-pointer hover:bg-zinc-900 transition-colors"
                    >
                      <Upload className="h-5 w-5 mr-2 text-zinc-400" />
                      <span className="text-zinc-300">Select kubeconfig files (.yaml, .yml)</span>
                    </label>

                    {pendingFiles.length > 0 && (
                      <div className="space-y-2">
                        {pendingFiles.map(f => (
                          <div key={f.filename} className="flex items-center justify-between p-3 bg-zinc-900 border border-zinc-800 rounded-lg">
                            <div className="flex items-center gap-2">
                              <FileText className="h-4 w-4 text-zinc-400" />
                              <span className="text-sm text-zinc-200">{f.filename}</span>
                              <span className="text-xs text-zinc-500">{(f.content.length / 1024).toFixed(1)}KB</span>
                            </div>
                            <Button variant="ghost" size="sm" onClick={() => removePendingFile(f.filename)} className="text-zinc-400 hover:text-red-400 h-7 w-7 p-0">
                              <X className="h-4 w-4" />
                            </Button>
                          </div>
                        ))}
                        <Button
                          onClick={uploadKubeconfigs}
                          disabled={uploading}
                          className="w-full bg-white text-black hover:bg-zinc-200"
                        >
                          {uploading ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" />Uploading...</> : `Upload ${pendingFiles.length} file(s)`}
                        </Button>
                      </div>
                    )}

                    {uploadError && (
                      <Alert className="bg-red-950/30 border-red-900/50">
                        <AlertCircle className="h-4 w-4 text-red-400" />
                        <AlertDescription className="text-sm text-red-300">{uploadError}</AlertDescription>
                      </Alert>
                    )}
                  </div>

                  <Alert className="bg-zinc-900 border-zinc-800">
                    <AlertCircle className="h-4 w-4 text-zinc-400" />
                    <AlertDescription className="text-xs text-zinc-400">
                      Kubeconfigs with auth-provider authentication are not supported.
                      Exec-based (gcloud, aws), certificate, and token auth all work.
                    </AlertDescription>
                  </Alert>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>
      </div>
      <Toaster />
    </ConnectorAuthGuard>
  );
}

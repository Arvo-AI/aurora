"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Check, Copy, Loader2, AlertCircle } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useToast } from "@/hooks/use-toast";
import { KUBECTL_AGENT } from "@/lib/kubectl-constants";

const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || '';
const wsUrl = process.env.NEXT_PUBLIC_WEBSOCKET_URL || '';
const wsEndpoint = wsUrl || backendUrl.replace(/^https?:\/\//, 'wss://').replace(/^http:\/\//, 'ws://');

const getHelmInstallCommand = (token: string) => `helm install ${KUBECTL_AGENT.RELEASE_NAME} ${KUBECTL_AGENT.CHART_OCI_URL} \\
  --version ${KUBECTL_AGENT.CHART_VERSION} \\
  --create-namespace \\
  --namespace ${KUBECTL_AGENT.DEFAULT_NAMESPACE} \\
  --set aurora.agentToken="${token}" \\
  --set aurora.backendUrl="${backendUrl}" \\
  --set aurora.wsEndpoint="${wsEndpoint}"`;

const connectivityCommand = `kubectl run aurora-egress-check --rm -i --tty --image=${KUBECTL_AGENT.EGRESS_CHECK_IMAGE} --restart=Never -- \\
  sh -c "wget -qO- ${backendUrl}/healthz"`;

const statusCommand = `kubectl get pods -n ${KUBECTL_AGENT.DEFAULT_NAMESPACE} -l ${KUBECTL_AGENT.POD_LABEL_SELECTOR}`;

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

  useEffect(() => {
    if (!generatedToken) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/kubectl/status?token=${encodeURIComponent(generatedToken)}`);
        const data = await res.json();
        if (data.connected) {
          setAgentStatus('connected');
          localStorage.setItem(KUBECTL_AGENT.STORAGE_KEY, 'true');
          window.dispatchEvent(new CustomEvent('providerStateChanged'));
        }
        // Stay in 'checking' state - keep showing "Waiting for agent..." while polling
      } catch (error) {
        console.error('Error checking kubectl status:', error);
        // Stay in 'checking' state even on error - keep polling
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [generatedToken]);

  const copyCommand = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied(null), KUBECTL_AGENT.COPY_FEEDBACK_DURATION);
    } catch (error) {
      console.error("Failed to copy", error);
    }
  };

  const generateToken = async () => {
    if (!clusterName.trim()) {
      toast({
        title: "Cluster name required",
        description: "Please provide a name for your cluster",
        variant: "destructive",
      });
      return;
    }
    
    setGeneratingToken(true);
    try {
      const response = await fetch('/api/kubectl/tokens', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          cluster_name: clusterName.trim(),
          notes: clusterMetadata.trim(),
        }),
      });

      if (!response.ok) {
        throw new Error('Failed to generate token');
      }

      const data = await response.json();
      setGeneratedToken(data.token);
      setShowTokenForm(false);
      
      toast({
        title: "Token generated",
        description: "Copy this token now - it won't be shown again!",
      });
    } catch (error) {
      console.error('Error generating token:', error);
      toast({
        title: "Error",
        description: "Failed to generate token. Please try again.",
        variant: "destructive",
      });
    } finally {
      setGeneratingToken(false);
    }
  };

  const CommandRow = ({ label, command, copyKey }: { label: string; command: string; copyKey: string }) => {
    const highlightCommand = (cmd: string) => {
      return cmd.split('\n').map((line, idx) => {
        const parts: JSX.Element[] = [];
        let remaining = line;
        let key = 0;

        [
          { regex: /\b(helm|kubectl)\s+\w+/, color: 'text-cyan-400' },
          { regex: /--[\w-]+/, color: 'text-purple-400' },
          { regex: /"[^"]+"/, color: 'text-green-400' },
        ].forEach(({ regex, color }) => {
          const newParts: JSX.Element[] = [];
          parts.length ? parts.forEach(part => {
            if (part.props.className === 'text-zinc-100') {
              const text = part.props.children;
              const match = regex.exec(text);
              if (match) {
                newParts.push(<span key={key++} className="text-zinc-100">{text.slice(0, match.index)}</span>);
                newParts.push(<span key={key++} className={color}>{match[0]}</span>);
                newParts.push(<span key={key++} className="text-zinc-100">{text.slice(match.index + match[0].length)}</span>);
              } else {
                newParts.push(part);
              }
            } else {
              newParts.push(part);
            }
          }) : (() => {
            const match = regex.exec(remaining);
            if (match) {
              if (match.index > 0) newParts.push(<span key={key++} className="text-zinc-100">{remaining.slice(0, match.index)}</span>);
              newParts.push(<span key={key++} className={color}>{match[0]}</span>);
              if (match.index + match[0].length < remaining.length) newParts.push(<span key={key++} className="text-zinc-100">{remaining.slice(match.index + match[0].length)}</span>);
              remaining = '';
            }
          })();
          if (newParts.length) parts.splice(0, parts.length, ...newParts);
        });

        return <div key={idx}>{parts.length ? parts : <span className="text-zinc-100">{line}</span>}</div>;
      });
    };

    return (
      <div className="space-y-2">
        <Label className="text-sm text-zinc-300">{label}</Label>
        <div className="relative">
          <pre className="overflow-auto rounded-lg bg-zinc-900 border border-zinc-800 p-2 pr-12 text-xs leading-relaxed font-mono">
            {highlightCommand(command)}
          </pre>
          <Button 
            variant="ghost" 
            size="sm" 
            onClick={() => copyCommand(command, copyKey)}
            className={`absolute right-1 text-zinc-400 hover:text-zinc-100 hover:bg-transparent ${command.includes('\n') ? 'top-1' : 'top-1/2 -translate-y-1/2'}`}
          >
            {copied === copyKey ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
          </Button>
        </div>
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-black">
      <div className="container mx-auto py-12 px-4 max-w-4xl">
        {/* Manage Clusters Button - Top Right */}
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

        {/* Header with centered logo */}
        <div className="mb-8 flex flex-col items-center text-center">
          <img src="/kubernetes_text.png" alt="Kubernetes" className="h-32 w-auto mb-4" />
          <p className="text-sm text-zinc-400 max-w-xl mb-1">
            Deploy the Aurora kubernetes agent inside your Kubernetes cluster. Credentials stay in-cluster.
          </p>
          <p className="text-xs text-zinc-500">
            All connections are secure and encrypted
          </p>
        </div>

        <div className="space-y-6">
          {/* Step 1: Generate Token */}
          <Card className="bg-zinc-950 border-zinc-800">
            <CardHeader className="pb-4">
              <CardTitle className="text-white text-lg tracking-tight">
                1. Generate Agent Token
              </CardTitle>
              <CardDescription className="text-zinc-400 text-sm leading-relaxed">
                Create an authentication token for your kubernetes agent. This token authenticates your agent to Aurora.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {!generatedToken && !showTokenForm && (
                <Button 
                  onClick={() => setShowTokenForm(true)}
                  className="bg-white text-black hover:bg-zinc-200"
                >
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
                      required
                    />
                    <p className="text-xs text-zinc-500 mt-1.5">
                      Use the same cluster name as in your alerting system (Datadog, PagerDuty, etc.) for seamless incident correlation
                    </p>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="clusterMetadata" className="text-white text-sm">
                      Cluster Context <span className="text-zinc-500 text-xs">(Optional)</span>
                    </Label>
                    <textarea
                      id="clusterMetadata"
                      placeholder="e.g., Production environment, US-East region, serves customer API traffic"
                      value={clusterMetadata}
                      onChange={(e) => setClusterMetadata(e.target.value)}
                      className="w-full h-20 px-3 py-2 rounded-md bg-zinc-900 border border-zinc-800 text-white placeholder:text-zinc-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-700 text-sm resize-none"
                    />
                    <p className="text-xs text-zinc-500 mt-1.5">
                      Add context about this cluster (environment, region, purpose) to help Aurora investigate issues more effectively
                    </p>
                  </div>
                  <div className="flex gap-3">
                    <Button 
                      onClick={generateToken} 
                      disabled={generatingToken || !clusterName.trim()}
                      className="bg-white text-black hover:bg-zinc-200"
                    >
                      {generatingToken ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Generating...
                        </>
                      ) : (
                        "Generate Token"
                      )}
                    </Button>
                    <Button 
                      variant="outline" 
                      onClick={() => setShowTokenForm(false)}
                      className="border-zinc-700 text-zinc-300 hover:bg-zinc-800"
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              {generatedToken && (
                <div className="space-y-4">
                  <Alert className="bg-zinc-900 border-zinc-700">
                    <AlertDescription className="text-zinc-300 text-sm">
                      Save this token now - it will only be shown once! You'll need it for the Helm installation.
                    </AlertDescription>
                  </Alert>
                  <div className="space-y-2">
                    <Label className="text-white text-sm">Your Agent Token</Label>
                    <div className="relative">
                      <Input
                        value={generatedToken}
                        readOnly
                        className="bg-zinc-900 border-zinc-800 text-white py-2 pr-12 font-mono text-xs"
                      />
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => copyCommand(generatedToken, "token")}
                        className="absolute top-1/2 -translate-y-1/2 right-1 text-zinc-400 hover:text-zinc-100 hover:bg-transparent"
                      >
                        {copied === "token" ? (
                          <Check className="h-4 w-4 text-green-400" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setGeneratedToken(null);
                      setClusterName("");
                    }}
                    className="border-zinc-700 text-zinc-300 hover:bg-zinc-800"
                  >
                    Generate Another Token
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Step 2: Set kubectl context */}
          <Card className="bg-zinc-950 border-zinc-800">
            <CardHeader className="pb-4">
              <CardTitle className="text-white text-lg tracking-tight">
                2. Set your kubectl context
              </CardTitle>
              <CardDescription className="text-zinc-400 text-sm leading-relaxed">
                The agent will be deployed to whichever cluster your kubectl is currently connected to. Verify you're connected to the correct cluster.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <CommandRow 
                label="Check current context" 
                command="kubectl config current-context" 
                copyKey="context-check" 
              />
              <CommandRow 
                label="List all contexts" 
                command="kubectl config get-contexts" 
                copyKey="context-list" 
              />
              <CommandRow 
                label="Switch context (if needed)" 
                command="kubectl config use-context <context-name>" 
                copyKey="context-switch" 
              />
              <p className="text-xs text-zinc-500 leading-relaxed">
                The Helm installation will deploy to the cluster configured in your current kubectl context.
              </p>
            </CardContent>
          </Card>

          {/* Step 3: Helm Install */}
          <Card className="bg-zinc-950 border-zinc-800">
            <CardHeader className="pb-4">
              <CardTitle className="text-white text-lg tracking-tight">
                3. Install the Aurora kubernetes agent with Helm
              </CardTitle>
              <CardDescription className="text-zinc-400 text-sm leading-relaxed">
                Deploy the agent into your cluster using the official Helm chart. The agent creates a ServiceAccount with read-only RBAC permissions and maintains an outbound connection to Aurora.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {!generatedToken && (
                <Alert className="bg-zinc-900 border-zinc-800">
                  <AlertCircle className="h-4 w-4 text-yellow-500" />
                  <AlertDescription className="text-sm text-zinc-300">
                    Generate a token in Step 1 first, then this command will automatically include it. The agent runs in the <code className="bg-zinc-900 px-1.5 py-0.5 rounded text-zinc-300 text-[11px] font-mono">default</code> namespace with read-only cluster access (you can change the namespace with <code className="bg-zinc-900 px-1.5 py-0.5 rounded text-zinc-300 text-[11px] font-mono">--namespace</code>). No credentials leave the cluster.
                  </AlertDescription>
                </Alert>
              )}
              {generatedToken && (
                <p className="text-sm text-zinc-400 leading-relaxed">
                  The command below includes your generated token. The agent runs in the <code className="bg-zinc-900 px-1.5 py-0.5 rounded text-zinc-300 text-[11px] font-mono">default</code> namespace with read-only cluster access (you can change the namespace with <code className="bg-zinc-900 px-1.5 py-0.5 rounded text-zinc-300 text-[11px] font-mono">--namespace</code>).
                </p>
              )}
              <CommandRow 
                label="Install with Helm" 
                command={getHelmInstallCommand(generatedToken || "<YOUR_TOKEN>")} 
                copyKey={generatedToken ? "helm" : "helm-placeholder"} 
              />
            </CardContent>
          </Card>

          {/* Step 4: Verify Connection */}
          <Card className="bg-zinc-950 border-zinc-800">
            <CardHeader className="pb-4">
              <CardTitle className="text-white text-lg tracking-tight">
                4. Verify agent connection
              </CardTitle>
              <CardDescription className="text-zinc-400 text-sm leading-relaxed">
                Aurora is monitoring for your agent connection. Once the agent is deployed and healthy, it will automatically connect.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {agentStatus === 'connected' ? (
                <div className="flex items-center gap-3 p-4 bg-green-950/30 border border-green-900/50 rounded-lg">
                  <Check className="h-5 w-5 text-green-400" />
                  <div>
                    <p className="text-green-400 text-sm">Agent Connected</p>
                    <p className="text-zinc-400 text-xs mt-0.5">Your kubernetes agent is connected and ready to use</p>
                  </div>
                </div>
              ) : agentStatus === 'checking' ? (
                <div className="flex items-center gap-3 p-4 bg-zinc-900 border border-zinc-800 rounded-lg">
                  <Loader2 className="h-5 w-5 text-zinc-400 animate-spin" />
                  <div>
                    <p className="text-zinc-300 text-sm">Waiting for agent...</p>
                    <p className="text-zinc-500 text-xs mt-0.5">Deploy the agent using Step 3, Aurora will detect it automatically</p>
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-3 p-4 bg-zinc-900 border border-zinc-800 rounded-lg">
                  <AlertCircle className="h-5 w-5 text-zinc-400" />
                  <div>
                    <p className="text-zinc-300 text-sm">No agent detected</p>
                    <p className="text-zinc-500 text-xs mt-0.5">Complete Step 3 to deploy the agent to your cluster</p>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}


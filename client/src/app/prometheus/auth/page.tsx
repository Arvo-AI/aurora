"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { prometheusService, PrometheusStatus } from "@/lib/services/prometheus";
import { getUserFriendlyError, copyToClipboard } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Copy, Check, ExternalLink, Shield, Unplug, ChevronDown, ChevronRight } from "lucide-react";

const CACHE_KEYS = {
  STATUS: 'prometheus_connection_status',
};

type CachedStatus = Pick<PrometheusStatus, 'connected' | 'prometheusUrl' | 'instanceLabel' | 'version'>;

const SETUP_METHODS = [
  { id: "helm", label: "Helm (kube-prometheus-stack)" },
  { id: "yaml", label: "alertmanager.yml" },
  { id: "operator", label: "Prometheus Operator CRD" },
  { id: "grafana", label: "Grafana UI" },
  { id: "test", label: "Verify & Test" },
] as const;

function WebhookSetupGuide({ webhookUrl }: { webhookUrl: string }) {
  const [activeMethod, setActiveMethod] = useState<string>("helm");
  const [expanded, setExpanded] = useState(true);

  return (
    <div className="border rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-3 text-sm font-medium hover:bg-muted/50 transition-colors"
      >
        <span>Setup Instructions</span>
        {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
      </button>

      {expanded && (
        <div className="border-t">
          {/* Method selector */}
          <div className="flex border-b overflow-x-auto">
            {SETUP_METHODS.map((method) => (
              <button
                key={method.id}
                type="button"
                onClick={() => setActiveMethod(method.id)}
                className={`px-4 py-2 text-xs font-medium whitespace-nowrap border-b-2 transition-colors ${
                  activeMethod === method.id
                    ? 'border-orange-500 text-orange-600 bg-orange-50 dark:bg-orange-950/30'
                    : 'border-transparent text-muted-foreground hover:text-foreground hover:bg-muted/50'
                }`}
              >
                {method.label}
              </button>
            ))}
          </div>

          {/* Instructions content */}
          <div className="p-4 text-sm space-y-3">
            {activeMethod === "helm" && (
              <>
                <p className="text-muted-foreground">
                  If you deployed Prometheus via the <code className="text-xs bg-muted px-1 py-0.5 rounded">kube-prometheus-stack</code> Helm chart, add Aurora as a receiver in your <code className="text-xs bg-muted px-1 py-0.5 rounded">values.yaml</code>:
                </p>
                <div className="space-y-2">
                  <p className="font-medium text-xs">1. Add to your Helm values:</p>
                  <pre className="bg-muted rounded p-3 text-xs font-mono overflow-x-auto">
{`alertmanager:
  config:
    receivers:
      - name: 'aurora'
        webhook_configs:
          - url: '${webhookUrl}'
            send_resolved: true
      - name: 'default'  # keep your existing default
    route:
      receiver: 'default'
      routes:
        - receiver: 'aurora'
          continue: true  # forward all alerts to Aurora
          match_re:
            alertname: '.*'`}
                  </pre>
                </div>
                <div className="space-y-2">
                  <p className="font-medium text-xs">2. Upgrade the release:</p>
                  <pre className="bg-muted rounded p-3 text-xs font-mono overflow-x-auto">
{`helm upgrade prometheus prometheus-community/kube-prometheus-stack \\
  -f values.yaml -n monitoring`}
                  </pre>
                </div>
                <p className="text-xs text-muted-foreground">
                  Using <code className="bg-muted px-1 py-0.5 rounded">continue: true</code> ensures alerts still reach your existing receivers (PagerDuty, Slack, etc.) alongside Aurora.
                </p>
              </>
            )}

            {activeMethod === "yaml" && (
              <>
                <p className="text-muted-foreground">
                  Edit your <code className="text-xs bg-muted px-1 py-0.5 rounded">alertmanager.yml</code> directly. This applies to standalone Alertmanager, VMAlertmanager, and Docker Compose setups.
                </p>
                <div className="space-y-2">
                  <p className="font-medium text-xs">Add Aurora as a receiver:</p>
                  <pre className="bg-muted rounded p-3 text-xs font-mono overflow-x-auto">
{`receivers:
  - name: 'aurora'
    webhook_configs:
      - url: '${webhookUrl}'
        send_resolved: true

route:
  # Option A: Send everything to Aurora
  receiver: 'aurora'

  # Option B: Keep existing receiver, add Aurora as a secondary route
  # receiver: 'your-existing-receiver'
  # routes:
  #   - receiver: 'aurora'
  #     continue: true
  #     match_re:
  #       alertname: '.*'`}
                  </pre>
                </div>
                <div className="space-y-2">
                  <p className="font-medium text-xs">Then reload Alertmanager:</p>
                  <pre className="bg-muted rounded p-3 text-xs font-mono overflow-x-auto">
{`# Hot reload (no downtime)
curl -X POST http://alertmanager:9093/-/reload

# Or restart the process/container
docker restart alertmanager`}
                  </pre>
                </div>
              </>
            )}

            {activeMethod === "operator" && (
              <>
                <p className="text-muted-foreground">
                  If you&apos;re using the Prometheus Operator, create an <code className="text-xs bg-muted px-1 py-0.5 rounded">AlertmanagerConfig</code> custom resource:
                </p>
                <div className="space-y-2">
                  <p className="font-medium text-xs">Apply this manifest:</p>
                  <pre className="bg-muted rounded p-3 text-xs font-mono overflow-x-auto">
{`apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: aurora-webhook
  namespace: monitoring
  labels:
    alertmanagerConfig: aurora
spec:
  receivers:
    - name: 'aurora'
      webhookConfigs:
        - url: '${webhookUrl}'
          sendResolved: true
  route:
    receiver: 'aurora'
    continue: true
    matchers: []  # match all alerts`}
                  </pre>
                </div>
                <p className="text-xs text-muted-foreground">
                  Make sure your Alertmanager CR has <code className="bg-muted px-1 py-0.5 rounded">alertmanagerConfigSelector</code> matching the labels above. The Operator will auto-merge this config.
                </p>
              </>
            )}

            {activeMethod === "grafana" && (
              <>
                <p className="text-muted-foreground">
                  If you manage alerting through Grafana (with an Alertmanager or Prometheus data source), you can add Aurora as a contact point directly in the Grafana UI.
                </p>
                <div className="space-y-3">
                  <div className="space-y-1">
                    <p className="font-medium text-xs">Steps</p>
                    <ol className="text-xs text-muted-foreground list-decimal pl-4 space-y-1">
                      <li>Go to <strong>Alerting → Contact points</strong></li>
                      <li>Click <strong>New contact point</strong></li>
                      <li>Name it <code className="bg-muted px-1 py-0.5 rounded">Aurora</code></li>
                      <li>Select integration type: <strong>Webhook</strong></li>
                      <li>Paste your webhook URL in the URL field</li>
                      <li>Click <strong>Save contact point</strong></li>
                      <li>Go to <strong>Notification policies</strong> → add Aurora as a receiver (use <code className="bg-muted px-1 py-0.5 rounded">continue</code> to keep existing receivers)</li>
                    </ol>
                  </div>
                </div>
              </>
            )}

            {activeMethod === "test" && (
              <>
                <p className="text-muted-foreground">
                  After configuring the webhook, test the full pipeline: Alertmanager → Aurora.
                </p>
                <div className="space-y-3">
                  <div className="space-y-1">
                    <p className="font-medium text-xs">1. Check Alertmanager loaded the config</p>
                    <p className="text-xs text-muted-foreground">
                      Open <code className="bg-muted px-1 py-0.5 rounded">http://your-alertmanager:9093/#/status</code> and confirm the <code className="bg-muted px-1 py-0.5 rounded">aurora</code> receiver appears. The Alertmanager UI is read-only — you can&apos;t edit config there, but you can verify it loaded.
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="font-medium text-xs">2. Send a test alert through Alertmanager</p>
                    <p className="text-xs text-muted-foreground mb-2">
                      This injects a test alert into Alertmanager, which will route it through all configured receivers — including the Aurora webhook. This tests the full pipeline end-to-end.
                    </p>
                    <pre className="bg-muted rounded p-3 text-xs font-mono overflow-x-auto">
{`# Send alert TO Alertmanager (replace with your Alertmanager URL)
curl -X POST 'http://your-alertmanager:9093/api/v2/alerts' \\
  -H 'Content-Type: application/json' \\
  -d '[{
    "labels": {
      "alertname": "AuroraTestAlert",
      "severity": "info",
      "source": "manual-test"
    },
    "annotations": {
      "summary": "Test alert to verify Aurora webhook delivery"
    }
  }]'`}
                    </pre>
                    <p className="text-xs text-muted-foreground">
                      Alertmanager should respond with <code className="bg-muted px-1 py-0.5 rounded">200 OK</code>. Within seconds, the alert will flow through Alertmanager&apos;s routing tree and arrive at Aurora. Check Aurora&apos;s incident pipeline to confirm.
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="font-medium text-xs">3. Or use amtool (if installed)</p>
                    <pre className="bg-muted rounded p-3 text-xs font-mono overflow-x-auto">
{`amtool alert add AuroraTestAlert severity=info source=manual-test \\
  --alertmanager.url=http://your-alertmanager:9093 \\
  --annotation.summary="Test alert for Aurora webhook"`}
                    </pre>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function PrometheusAuthPage() {
  const { toast } = useToast();
  const [prometheusUrl, setPrometheusUrl] = useState("");
  const [instanceLabel, setInstanceLabel] = useState("default");
  const [alertmanagerUrl, setAlertmanagerUrl] = useState("");
  const [authType, setAuthType] = useState("none");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [bearerToken, setBearerToken] = useState("");
  const [customHeaderKey, setCustomHeaderKey] = useState("");
  const [customHeaderValue, setCustomHeaderValue] = useState("");
  const [verifySsl, setVerifySsl] = useState(true);
  const [status, setStatus] = useState<PrometheusStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const updateLocalStorageConnection = (connected: boolean) => {
    if (typeof window === 'undefined') return;
    if (connected) {
      localStorage.setItem('isPrometheusConnected', 'true');
    } else {
      localStorage.removeItem('isPrometheusConnected');
    }
    window.dispatchEvent(new CustomEvent('providerStateChanged'));
  };

  const loadWebhookUrl = async () => {
    try {
      const response = await prometheusService.getWebhookUrl();
      setWebhookUrl(response.webhookUrl);
    } catch (error: unknown) {
      console.error('[prometheus] Failed to load webhook URL', error);
    }
  };

  const fetchAndUpdateStatus = async () => {
    const result = await prometheusService.getStatus();
    setStatus(result);

    if (typeof window !== 'undefined' && result) {
      const cached: CachedStatus = {
        connected: result.connected,
        prometheusUrl: result.prometheusUrl,
        instanceLabel: result.instanceLabel,
        version: result.version,
      };
      localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(cached));
    }

    updateLocalStorageConnection(result?.connected ?? false);

    if (result?.connected) {
      await loadWebhookUrl();
    } else if (typeof window !== 'undefined') {
      localStorage.removeItem(CACHE_KEYS.STATUS);
    }
  };

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== 'undefined') {
        const cachedStatus = localStorage.getItem(CACHE_KEYS.STATUS);

        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus) as CachedStatus;
          setStatus(parsedStatus as PrometheusStatus);
          updateLocalStorageConnection(parsedStatus?.connected ?? false);

          if (isInitialLoad) {
            setIsInitialLoad(false);
            fetchAndUpdateStatus();
            return;
          }
          return;
        }
      }

      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error('[prometheus] Failed to load status', error);
      toast({ title: 'Error', description: 'Unable to load Prometheus status', variant: 'destructive' });
    }
  };

  useEffect(() => {
    loadStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload: Record<string, unknown> = {
        prometheusUrl,
        instanceLabel: instanceLabel || "default",
        alertmanagerUrl: alertmanagerUrl || undefined,
        authType,
        verifySsl,
      };

      if (authType === "basic") {
        payload.username = username;
        payload.password = password;
      } else if (authType === "bearer") {
        payload.bearerToken = bearerToken;
      } else if (authType === "custom" && customHeaderKey) {
        payload.customHeaders = { [customHeaderKey]: customHeaderValue };
      }

      const result = await prometheusService.connect(payload as Parameters<typeof prometheusService.connect>[0]);
      setStatus(result);

      if (typeof window !== 'undefined') {
        const cached: CachedStatus = {
          connected: true,
          prometheusUrl: result.prometheusUrl,
          instanceLabel: result.instanceLabel,
          version: result.version,
        };
        localStorage.setItem(CACHE_KEYS.STATUS, JSON.stringify(cached));
        localStorage.setItem('isPrometheusConnected', 'true');
      }

      toast({
        title: 'Success',
        description: 'Prometheus connected successfully. Configure the Alertmanager webhook below to start receiving alerts.',
      });

      await loadWebhookUrl();
      updateLocalStorageConnection(true);

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'add', provider: 'prometheus' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: ['prometheus'] } }));
      } catch (prefErr: unknown) {
        console.warn('[prometheus] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[prometheus] Connect failed', error);
      const message = getUserFriendlyError(error);
      toast({ title: 'Failed to connect to Prometheus', description: message, variant: 'destructive' });
    } finally {
      setLoading(false);
      setPassword('');
      setBearerToken('');
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/connected-accounts/prometheus', {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok && response.status !== 204) {
        const text = await response.text();
        throw new Error(text || 'Failed to disconnect Prometheus');
      }

      setStatus({ connected: false });
      setWebhookUrl(null);
      setPrometheusUrl('');
      setInstanceLabel('default');
      setAlertmanagerUrl('');
      setAuthType('none');

      if (typeof window !== 'undefined') {
        localStorage.removeItem(CACHE_KEYS.STATUS);
        localStorage.removeItem('isPrometheusConnected');
      }

      updateLocalStorageConnection(false);
      toast({ title: 'Success', description: 'Prometheus disconnected successfully.' });

      try {
        await fetch('/api/provider-preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', provider: 'prometheus' }),
        });
        window.dispatchEvent(new CustomEvent('providerPreferenceChanged', { detail: { providers: [] } }));
      } catch (prefErr: unknown) {
        console.warn('[prometheus] Failed to update provider preferences', prefErr);
      }
    } catch (error: unknown) {
      console.error('[prometheus] Disconnect failed', error);
      const message = getUserFriendlyError(error);
      toast({ title: 'Failed to disconnect Prometheus', description: message, variant: 'destructive' });
    } finally {
      setLoading(false);
    }
  };

  const handleCopyWebhook = () => {
    if (!webhookUrl) return;
    copyToClipboard(webhookUrl);
    setCopied(true);
    toast({ title: 'Copied', description: 'Webhook URL copied to clipboard' });
    setTimeout(() => setCopied(false), 2000);
  };

  const isConnected = Boolean(status?.connected);

  return (
    <ConnectorAuthGuard connectorName="Prometheus">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Prometheus Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect Prometheus to query metrics via PromQL and receive Alertmanager alerts for automated root cause analysis.
            Also supports Thanos, Cortex, Mimir, and VictoriaMetrics.
          </p>
        </div>

        <div className="flex items-center justify-center mb-8">
          <div className="flex items-center">
            <div className={`flex items-center justify-center w-10 h-10 rounded-full ${!isConnected ? 'bg-orange-500 text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
              1
            </div>
            <div className={`w-24 h-1 ${isConnected ? 'bg-orange-500' : 'bg-gray-200'}`}></div>
            <div className={`flex items-center justify-center w-10 h-10 rounded-full ${isConnected ? 'bg-orange-500 text-white' : 'bg-gray-200 text-gray-600'} font-bold`}>
              2
            </div>
          </div>
        </div>

        <div className="flex items-center justify-center mb-6 text-sm font-medium">
          <span className={!isConnected ? 'text-orange-500' : 'text-muted-foreground'}>
            Connect Prometheus
          </span>
          <span className="mx-4 text-muted-foreground">&rarr;</span>
          <span className={isConnected ? 'text-orange-500' : 'text-muted-foreground'}>
            Configure Alertmanager Webhook
          </span>
        </div>

        {!isConnected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect to Prometheus</CardTitle>
              <CardDescription>
                Enter your Prometheus server URL and authentication details.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleConnect} className="space-y-6">
                <div className="space-y-2">
                  <Label htmlFor="prometheusUrl">Prometheus URL *</Label>
                  <Input
                    id="prometheusUrl"
                    type="url"
                    placeholder="https://prometheus.example.com:9090"
                    value={prometheusUrl}
                    onChange={(e) => setPrometheusUrl(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    The base URL of your Prometheus server (or Thanos/Mimir/VictoriaMetrics query endpoint).
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="instanceLabel">Instance Label</Label>
                  <Input
                    id="instanceLabel"
                    type="text"
                    placeholder="us-east-prod"
                    value={instanceLabel}
                    onChange={(e) => setInstanceLabel(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    A friendly name to identify this instance (e.g. &quot;us-east-prod&quot;, &quot;staging&quot;).
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="alertmanagerUrl">Alertmanager URL (optional)</Label>
                  <Input
                    id="alertmanagerUrl"
                    type="url"
                    placeholder="https://alertmanager.example.com:9093"
                    value={alertmanagerUrl}
                    onChange={(e) => setAlertmanagerUrl(e.target.value)}
                  />
                </div>

                <div className="space-y-3">
                  <Label>Authentication</Label>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {[
                      { value: "none", label: "None" },
                      { value: "basic", label: "Username & Password" },
                      { value: "bearer", label: "Bearer Token" },
                      { value: "custom", label: "Custom Headers" },
                    ].map((opt) => (
                      <button
                        key={opt.value}
                        type="button"
                        className={`px-3 py-2 rounded-md border text-sm font-medium transition-colors ${
                          authType === opt.value
                            ? 'border-orange-500 bg-orange-50 text-orange-700 dark:bg-orange-950 dark:text-orange-300'
                            : 'border-border hover:bg-muted'
                        }`}
                        onClick={() => setAuthType(opt.value)}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>

                {authType === "basic" && (
                  <div className="space-y-3 pl-4 border-l-2 border-orange-200">
                    <div className="space-y-2">
                      <Label htmlFor="username">Username</Label>
                      <Input
                        id="username"
                        type="text"
                        placeholder="prometheus"
                        value={username}
                        onChange={(e) => setUsername(e.target.value)}
                        required
                      />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="password">Password</Label>
                      <Input
                        id="password"
                        type="password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        required
                      />
                    </div>
                  </div>
                )}

                {authType === "bearer" && (
                  <div className="space-y-3 pl-4 border-l-2 border-orange-200">
                    <div className="space-y-2">
                      <Label htmlFor="bearerToken">API Token</Label>
                      <Input
                        id="bearerToken"
                        type="password"
                        placeholder="your-api-token"
                        value={bearerToken}
                        onChange={(e) => setBearerToken(e.target.value)}
                        required
                      />
                      <p className="text-xs text-muted-foreground">
                        Used for Grafana Cloud, managed Prometheus services, or any endpoint using Bearer auth.
                      </p>
                    </div>
                  </div>
                )}

                {authType === "custom" && (
                  <div className="space-y-3 pl-4 border-l-2 border-orange-200">
                    <div className="space-y-2">
                      <Label htmlFor="headerKey">Header Name</Label>
                      <Input
                        id="headerKey"
                        type="text"
                        placeholder="X-Scope-OrgID"
                        value={customHeaderKey}
                        onChange={(e) => setCustomHeaderKey(e.target.value)}
                        required
                      />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="headerValue">Header Value</Label>
                      <Input
                        id="headerValue"
                        type="text"
                        placeholder="tenant-1"
                        value={customHeaderValue}
                        onChange={(e) => setCustomHeaderValue(e.target.value)}
                        required
                      />
                    </div>
                    <p className="text-xs text-muted-foreground">
                      For Cortex/Mimir multi-tenancy (X-Scope-OrgID) or any custom auth header.
                    </p>
                  </div>
                )}

                <div className="flex items-center space-x-2">
                  <input
                    id="verifySsl"
                    type="checkbox"
                    checked={verifySsl}
                    onChange={(e) => setVerifySsl(e.target.checked)}
                    className="rounded border-gray-300"
                  />
                  <Label htmlFor="verifySsl" className="text-sm font-normal">
                    Verify SSL certificate
                  </Label>
                </div>

                <Button type="submit" disabled={loading || !prometheusUrl} className="w-full bg-orange-500 hover:bg-orange-600">
                  {loading ? "Connecting..." : "Connect Prometheus"}
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-6">
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle className="flex items-center gap-2">
                      <Shield className="w-5 h-5 text-green-500" />
                      Connected
                    </CardTitle>
                    <CardDescription>
                      Prometheus is connected and ready to use.
                    </CardDescription>
                  </div>
                  <Badge variant="secondary">{status?.version || "Prometheus"}</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="grid gap-3 text-sm">
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">URL</span>
                    <span className="font-mono text-xs">{status?.prometheusUrl}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Instance</span>
                    <span>{status?.instanceLabel || "default"}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Auth Type</span>
                    <span className="capitalize">{status?.authType || "none"}</span>
                  </div>
                  {status?.validatedAt && (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Connected At</span>
                      <span>{new Date(status.validatedAt).toLocaleDateString()}</span>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>

            {webhookUrl && (
              <Card>
                <CardHeader>
                  <CardTitle>Configure Alertmanager Webhook</CardTitle>
                  <CardDescription>
                    Point your Alertmanager at this URL so Aurora receives alerts in real-time.
                    This works with Prometheus Alertmanager, VMAlertmanager, Thanos, and Mimir.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-6">
                  {/* Webhook URL with copy */}
                  <div>
                    <Label className="text-xs text-muted-foreground mb-1 block">Your webhook URL</Label>
                    <div className="flex items-center gap-2">
                      <code className="flex-1 text-xs bg-muted p-3 rounded-md font-mono break-all">
                        {webhookUrl}
                      </code>
                      <Button variant="outline" size="icon" onClick={handleCopyWebhook}>
                        {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                      </Button>
                    </div>
                  </div>

                  {/* Setup method tabs */}
                  <WebhookSetupGuide webhookUrl={webhookUrl} />

                  <div className="border-t pt-4 flex items-center gap-4 text-sm">
                    <a
                      href="https://prometheus.io/docs/alerting/latest/configuration/#webhook_config"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-orange-500 hover:underline"
                    >
                      Alertmanager docs <ExternalLink className="w-3 h-3" />
                    </a>
                    <a
                      href="https://docs.victoriametrics.com/vmalert/#notifier-configuration-file"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-orange-500 hover:underline"
                    >
                      VMAlert docs <ExternalLink className="w-3 h-3" />
                    </a>
                  </div>
                </CardContent>
              </Card>
            )}

            <Button
              variant="destructive"
              onClick={handleDisconnect}
              disabled={loading}
              className="w-full"
            >
              <Unplug className="w-4 h-4 mr-2" />
              {loading ? "Disconnecting..." : "Disconnect Prometheus"}
            </Button>
          </div>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}

"use client";

import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { LokiStatus } from "@/lib/services/loki";

interface LokiWebhookStepProps {
  status: LokiStatus;
  webhookUrl: string;
  instructions: string[];
  copiedField: "url" | null;
  onCopyUrl: () => void;
  onDisconnect: () => void;
  loading: boolean;
}

export function LokiWebhookStep({
  status,
  webhookUrl,
  copiedField,
  onCopyUrl,
  onDisconnect,
  loading,
}: LokiWebhookStepProps) {
  const router = useRouter();

  return (
    <div className="space-y-6">
      {/* Connection Status Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Loki Connected</CardTitle>
              <CardDescription>Your Loki instance is successfully connected</CardDescription>
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => router.push("/loki/alerts")}>
                View Alerts
              </Button>
              <Button variant="destructive" onClick={onDisconnect} disabled={loading}>
                Disconnect
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {status.baseUrl && (
            <div className="flex justify-between py-1 border-b">
              <span className="font-medium">URL:</span>
              <span className="text-muted-foreground">{status.baseUrl}</span>
            </div>
          )}
          {status.authType && (
            <div className="flex justify-between py-1 border-b">
              <span className="font-medium">Auth:</span>
              <span className="text-muted-foreground">
                {status.authType.charAt(0).toUpperCase() + status.authType.slice(1)}
              </span>
            </div>
          )}
          {status.tenantId && (
            <div className="flex justify-between py-1">
              <span className="font-medium">Tenant:</span>
              <span className="text-muted-foreground">{status.tenantId}</span>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Webhook Configuration Card */}
      <Card>
        <CardHeader>
          <CardTitle>Configure Alert Webhook</CardTitle>
          <CardDescription>Add Aurora as a webhook destination for Loki Ruler alerts</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Webhook URL */}
          <div>
            <Label className="text-base font-semibold mb-2 block">Webhook URL</Label>
            <div className="flex items-center gap-2 mt-2">
              <code className="flex-1 px-4 py-3 bg-muted rounded text-sm font-mono break-all">
                {webhookUrl}
              </code>
              <Button variant="outline" onClick={onCopyUrl} className="flex-shrink-0">
                {copiedField === "url" ? "Copied!" : "Copy"}
              </Button>
            </div>
          </div>

          {/* Setup Instructions */}
          <div className="space-y-4 text-sm">
            {/* Option A: Alertmanager Receiver */}
            <div className="space-y-3">
              <p className="font-semibold">Option A: Alertmanager Receiver</p>
              <div className="space-y-2">
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">1.</span>
                  <p>
                    Set <code className="px-1.5 py-0.5 bg-muted rounded text-xs">alertmanager_url</code> in
                    your Loki ruler config to point to an Alertmanager instance
                  </p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">2.</span>
                  <p>In Alertmanager, add a webhook receiver with the URL above</p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">3.</span>
                  <p>Example Alertmanager config:</p>
                </div>
              </div>
              <pre className="px-4 py-3 bg-muted rounded text-xs font-mono overflow-x-auto whitespace-pre">
{`receivers:
  - name: aurora-webhook
    webhook_configs:
      - url: '${webhookUrl}'`}
              </pre>
            </div>

            {/* Option B: Grafana Alerting Contact Point */}
            <div className="space-y-3">
              <p className="font-semibold">Option B: Grafana Alerting Contact Point</p>
              <div className="space-y-2">
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">1.</span>
                  <p>In Grafana, go to <strong>Alerting</strong> &gt; <strong>Contact points</strong></p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">2.</span>
                  <p>Add a new contact point with type <strong>Webhook</strong></p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">3.</span>
                  <p>Paste the webhook URL above</p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="text-muted-foreground mt-0.5">4.</span>
                  <p>Route Loki-sourced alert rules to this contact point</p>
                </div>
              </div>
            </div>
          </div>

          <div className="p-3 bg-green-50 dark:bg-green-950/20 border border-green-200 dark:border-green-800 rounded">
            <p className="text-xs text-green-800 dark:text-green-400">
              Aurora will receive alerts from Loki once the webhook is configured.
            </p>
          </div>

          <div className="p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
            <a
              href="https://grafana.com/docs/loki/latest/alert/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
            >
              View Loki Alerting Documentation
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
            </a>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { DatadogStatus } from "@/lib/services/datadog";

interface DatadogWebhookStepProps {
  status: DatadogStatus;
  webhookUrl: string | null;
  copied: boolean;
  onCopy: () => void;
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

export function DatadogWebhookStep({ status, webhookUrl, copied, onCopy, onDisconnect, loading }: DatadogWebhookStepProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 2: Configure Datadog Webhook</CardTitle>
        <CardDescription>Send monitor incidents and events directly into Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid md:grid-cols-3 gap-4">
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Datadog Site</p>
            <p className="text-base font-semibold">{status.site || 'datadoghq.com'}</p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Service Account</p>
            <p className="text-base font-semibold">{status.serviceAccountName || 'Not provided'}</p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Org</p>
            <p className="text-base font-semibold">{status.org?.name || 'Datadog'}</p>
          </div>
        </div>

        {webhookUrl && (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium">Webhook URL</p>
              <Badge variant="outline">Per user</Badge>
            </div>
            <div className="flex flex-col md:flex-row md:items-center gap-3">
              <code className="flex-1 px-3 py-2 rounded bg-muted text-xs break-all border">{webhookUrl}</code>
              <Button variant={copied ? "secondary" : "default"} onClick={onCopy}>
                {copied ? "Copied" : "Copy URL"}
              </Button>
            </div>
          </div>
        )}

        <div className="space-y-3">
          <p className="text-sm font-medium">Add to Datadog:</p>
          <ol className="list-decimal list-inside space-y-2 text-sm text-muted-foreground">
            <li>Go to <strong>Integrations</strong> and search up <strong>Webhooks</strong> integration by Datadog.</li>
            <li>Create a webhook target using the URL above. Optionally set a shared secret for signing.</li>
            <li>In any monitor notification, include <code className="bg-muted px-1 rounded">@webhook-{"<your_webhook_name>"}</code> so Datadog knows where to send alerts.</li>
            <li>Navigate to <strong>Monitors → New Monitor → Metric</strong>, set any test condition (e.g., system.cpu.idle &lt; 100), then in the "Notify your team" section add @webhook-&lt;your_webhook_name&gt; and click <strong>Test notifications</strong> to send a sample alert to Aurora.</li>
          </ol>
        </div>

        <div className="border rounded-lg p-4 bg-muted/40 space-y-3 text-sm text-muted-foreground">
          <p className="font-medium text-foreground">Verify the webhook in Datadog</p>
          <ol className="list-decimal list-inside space-y-2">
            <li>
              Go to <strong>Monitors → New Monitor → Metric</strong> (any metric works) and set an easy condition, e.g.
              <code className="bg-muted px-1 rounded mx-1">system.cpu.idle &lt; 100</code>.
            </li>
            <li>
              In the notification message, include your webhook reference:
              <code className="bg-muted px-1 rounded mx-1">@webhook-{"<your_webhook_name>"}</code>.
            </li>
            <li>
              Click <strong>Test notifications</strong> inside the monitor. Datadog sends a sample payload to the URL above.
            </li>
            <li>
              Watch the <a className="underline" href="/datadog/events">Datadog Webhook Events</a> page (or your backend logs) to confirm the test alert appears.
            </li>
          </ol>
        </div>

        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="text-xs text-muted-foreground">
            Connected since <strong>{status.validatedAt ? new Date(status.validatedAt).toLocaleString() : 'just now'}</strong>
          </div>
          <Button variant="outline" onClick={onDisconnect} disabled={loading}>
            {loading ? "Disconnecting…" : "Disconnect Datadog"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

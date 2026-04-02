"use client";

import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { GrafanaStatus } from "@/lib/services/grafana";
import { CheckCircle2, Copy, ExternalLink } from "lucide-react";

interface GrafanaWebhookStepProps {
  status: GrafanaStatus;
  webhookUrl: string;
  copied: boolean;
  onCopy: () => void;
  onDisconnect: () => void;
  loading: boolean;
}

export function GrafanaWebhookStep({
  status,
  webhookUrl,
  copied,
  onCopy,
  onDisconnect,
  loading,
}: GrafanaWebhookStepProps) {
  const router = useRouter();

  const label = status.org?.name
    ? `Connected to ${status.org.name}`
    : status.baseUrl
      ? `Connected to ${(() => { try { return new URL(status.baseUrl).hostname; } catch { return status.baseUrl; } })()}`
      : "Your Grafana instance is connected";

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <CheckCircle2 className="h-5 w-5 text-green-500" />
            Grafana Connected
          </CardTitle>
          <CardDescription>{label}</CardDescription>
        </CardHeader>
        <CardContent className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/grafana/alerts")}>
            View Alerts
          </Button>
          <Button variant="destructive" onClick={onDisconnect} disabled={loading}>
            {loading ? "Disconnecting..." : "Disconnect"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Webhook Configuration</CardTitle>
          <CardDescription>
            Configure Grafana to send alert notifications to Aurora.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label>Webhook URL</Label>
            <div className="flex gap-2">
              <Input readOnly value={webhookUrl} className="font-mono text-sm" />
              <Button variant="outline" size="icon" onClick={onCopy}>
                {copied ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>
          </div>

          <div className="bg-muted/50 rounded-lg p-4 text-sm">
            <p className="font-medium mb-2">Setup instructions:</p>
            <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
              <li>Go to Alerts &amp; IRM &gt; Notification Configuration &gt; Contact points in Grafana</li>
              <li>Click <strong className="text-foreground">New contact point</strong></li>
              <li>Select <strong className="text-foreground">Webhook</strong> as the integration type</li>
              <li>Paste the webhook URL above into the URL field</li>
              <li>Click <strong className="text-foreground">Test</strong> to verify, then save</li>
              <li>Add the contact point to a notification policy under Notification Configuration &gt; Notification policies</li>
            </ol>
            <a
              href="https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-3 text-xs"
            >
              Grafana webhook docs <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

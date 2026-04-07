"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CheckCircle2, Copy, ExternalLink, Loader2 } from "lucide-react";
import { grafanaService } from "@/lib/services/grafana";
import { copyToClipboard } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";

interface GrafanaConnectionStepProps {
  onConnected: () => void;
}

export function GrafanaConnectionStep({ onConnected }: GrafanaConnectionStepProps) {
  const { toast } = useToast();
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [polling, setPolling] = useState(false);

  useEffect(() => {
    grafanaService.getWebhookUrl().then(res => setWebhookUrl(res.webhookUrl)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!webhookUrl) return;
    setPolling(true);
    const interval = setInterval(async () => {
      try {
        const status = await grafanaService.getStatus();
        if (status?.connected) {
          clearInterval(interval);
          setPolling(false);
          onConnected();
        }
      } catch {}
    }, 4000);
    return () => clearInterval(interval);
  }, [webhookUrl, onConnected]);

  const handleCopy = () => {
    if (!webhookUrl) return;
    copyToClipboard(webhookUrl);
    setCopied(true);
    toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Connect Grafana</CardTitle>
        <CardDescription>
          Configure a webhook contact point in Grafana and send a test notification to connect.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {webhookUrl ? (
          <>
            <div className="space-y-2">
              <Label>Your Webhook URL</Label>
              <div className="flex gap-2">
                <Input readOnly value={webhookUrl} className="font-mono text-sm" />
                <Button variant="outline" size="icon" onClick={handleCopy}>
                  {copied ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
            </div>

            <div className="bg-muted/50 rounded-lg p-4 text-sm">
              <p className="font-medium mb-2">Setup instructions:</p>
              <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                <li>Go to <strong className="text-foreground">Alerts &amp; IRM &gt; Notification Configuration &gt; Contact points</strong> in Grafana</li>
                <li>Click <strong className="text-foreground">New contact point</strong></li>
                <li>Select <strong className="text-foreground">Webhook</strong> as the integration type</li>
                <li>Paste the webhook URL above into the URL field</li>
                <li>Click <strong className="text-foreground">Test</strong> to send a test notification</li>
                <li>Save the contact point and add it to a notification policy</li>
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

            {polling && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground pt-1">
                <Loader2 className="h-4 w-4 animate-spin" />
                Waiting for test webhook from Grafana...
              </div>
            )}
          </>
        ) : (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading webhook URL...
          </div>
        )}
      </CardContent>
    </Card>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { CheckCircle2, Copy, ExternalLink, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { dynatraceService, DynatraceStatus, DynatraceWebhookUrlResponse } from "@/lib/services/dynatrace";

interface DynatraceWebhookStepProps {
  status: DynatraceStatus;
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

export function DynatraceWebhookStep({ status, onDisconnect, loading }: DynatraceWebhookStepProps) {
  const router = useRouter();
  const { toast } = useToast();
  const [webhookData, setWebhookData] = useState<DynatraceWebhookUrlResponse | null>(null);
  const [loadingWebhook, setLoadingWebhook] = useState(true);
  const [rcaEnabled, setRcaEnabled] = useState(false);
  const [loadingRcaSettings, setLoadingRcaSettings] = useState(true);
  const [updatingRca, setUpdatingRca] = useState(false);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const [wh, rca] = await Promise.all([
          dynatraceService.getWebhookUrl(),
          dynatraceService.getRcaSettings(),
        ]);
        if (mounted) {
          setWebhookData(wh);
          setRcaEnabled(rca.rcaEnabled);
        }
      } catch (error) {
        console.error("Failed to load Dynatrace settings:", error);
      } finally {
        if (mounted) {
          setLoadingWebhook(false);
          setLoadingRcaSettings(false);
        }
      }
    })();
    return () => { mounted = false; };
  }, []);

  const handleRcaToggle = async (enabled: boolean) => {
    setUpdatingRca(true);
    try {
      const result = await dynatraceService.updateRcaSettings(enabled);
      setRcaEnabled(result.rcaEnabled);
      toast({
        title: enabled ? "Alert RCA Enabled" : "Alert RCA Disabled",
        description: enabled
          ? "Aurora will automatically investigate Dynatrace problems"
          : "Dynatrace problems will not trigger automatic investigation",
      });
    } catch {
      toast({ title: "Failed to update settings", variant: "destructive" });
    } finally {
      setUpdatingRca(false);
    }
  };

  const copyWebhookUrl = async () => {
    if (!webhookData?.webhookUrl) return;
    try {
      await navigator.clipboard.writeText(webhookData.webhookUrl);
      toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
    } catch {
      toast({ title: "Copy failed", variant: "destructive" });
    }
  };

  const copyPayload = async () => {
    if (!webhookData?.suggestedPayload) return;
    try {
      await navigator.clipboard.writeText(webhookData.suggestedPayload);
      toast({ title: "Copied", description: "Payload template copied to clipboard" });
    } catch {
      toast({ title: "Copy failed", variant: "destructive" });
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2 className="h-5 w-5 text-green-500" />
          Connected to Dynatrace
        </CardTitle>
        <CardDescription>
          Your Dynatrace environment is connected. Configure webhooks to receive problem notifications.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="bg-muted/50 rounded-lg p-4 space-y-2">
          <div className="flex justify-between">
            <span className="text-sm text-muted-foreground">Environment URL</span>
            <span className="text-sm font-medium">{status.environmentUrl}</span>
          </div>
          {status.version && (
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Version</span>
              <span className="text-sm font-medium">{status.version}</span>
            </div>
          )}
        </div>

        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/dynatrace/alerts")}>View Alerts</Button>
        </div>

        <div className="border-t pt-6">
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label htmlFor="rca-toggle" className="text-base font-medium">Enable Alert RCA</Label>
              <p className="text-sm text-muted-foreground">Automatically investigate Dynatrace problems with Aurora</p>
            </div>
            {loadingRcaSettings ? (
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            ) : (
              <Switch id="rca-toggle" checked={rcaEnabled} onCheckedChange={handleRcaToggle} disabled={updatingRca} />
            )}
          </div>
        </div>

        {rcaEnabled && (
          <div className="border-t pt-6">
            <h3 className="font-medium mb-4">Configure Problem Notification Webhook</h3>
            {loadingWebhook ? (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading webhook URL...
              </div>
            ) : webhookData ? (
              <div className="space-y-4">
                <div>
                  <label className="text-sm font-medium">Webhook URL</label>
                  <div className="flex gap-2 mt-1">
                    <code className="flex-1 p-2 bg-muted rounded text-xs break-all">{webhookData.webhookUrl}</code>
                    <Button variant="outline" size="icon" onClick={copyWebhookUrl}><Copy className="h-4 w-4" /></Button>
                  </div>
                </div>

                <div>
                  <div className="flex items-center justify-between">
                    <label className="text-sm font-medium">Custom Payload Template</label>
                    <Button variant="ghost" size="sm" onClick={copyPayload} className="h-7 text-xs">
                      <Copy className="h-3 w-3 mr-1" /> Copy
                    </Button>
                  </div>
                  <pre className="mt-1 p-3 bg-muted rounded text-xs overflow-auto max-h-48">{webhookData.suggestedPayload}</pre>
                </div>

                <div className="bg-muted/50 rounded-lg p-4">
                  <p className="font-medium text-sm mb-3">Setup Instructions:</p>
                  <ol className="list-decimal list-inside space-y-2 text-sm text-muted-foreground">
                    {webhookData.instructions.map((instruction, idx) => (
                      <li key={idx}>{instruction.replace(/^\d+\.\s*/, '')}</li>
                    ))}
                  </ol>
                </div>

                <a
                  href="https://docs.dynatrace.com/docs/analyze-explore-automate/notifications-and-alerting/problem-notifications/webhook-integration"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-sm text-blue-600 hover:underline"
                >
                  View Dynatrace Webhook Documentation <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Failed to load webhook URL</p>
            )}
          </div>
        )}

        <div className="border-t pt-6">
          <Button variant="destructive" onClick={onDisconnect} disabled={loading} className="w-full">
            {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Disconnecting...</> : "Disconnect Dynatrace"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

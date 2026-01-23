"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { CheckCircle2, Copy, ExternalLink, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { splunkService, SplunkStatus, SplunkWebhookUrlResponse } from "@/lib/services/splunk";

interface SplunkWebhookStepProps {
  status: SplunkStatus;
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

export function SplunkWebhookStep({ status, onDisconnect, loading }: SplunkWebhookStepProps) {
  const router = useRouter();
  const { toast } = useToast();
  const [webhookData, setWebhookData] = useState<SplunkWebhookUrlResponse | null>(null);
  const [loadingWebhook, setLoadingWebhook] = useState(true);
  const [rcaEnabled, setRcaEnabled] = useState(false);
  const [loadingRcaSettings, setLoadingRcaSettings] = useState(true);
  const [updatingRca, setUpdatingRca] = useState(false);

  useEffect(() => {
    let isMounted = true;

    const loadData = async () => {
      setLoadingWebhook(true);
      setLoadingRcaSettings(true);

      try {
        const [webhookResponse, rcaSettings] = await Promise.all([
          splunkService.getWebhookUrl(),
          splunkService.getRcaSettings(),
        ]);

        if (isMounted) {
          setWebhookData(webhookResponse);
          setRcaEnabled(rcaSettings.rcaEnabled);
        }
      } catch (error) {
        console.error("Failed to load Splunk settings:", error);
      } finally {
        if (isMounted) {
          setLoadingWebhook(false);
          setLoadingRcaSettings(false);
        }
      }
    };

    loadData();

    return () => {
      isMounted = false;
    };
  }, []);

  const handleRcaToggle = async (enabled: boolean) => {
    setUpdatingRca(true);
    try {
      const result = await splunkService.updateRcaSettings(enabled);
      setRcaEnabled(result.rcaEnabled);
      toast({
        title: enabled ? "Alert RCA Enabled" : "Alert RCA Disabled",
        description: enabled
          ? "Aurora will automatically investigate Splunk alerts"
          : "Splunk alerts will not trigger automatic investigation",
      });
    } catch (error) {
      console.error("Failed to update RCA settings:", error);
      toast({
        title: "Failed to update settings",
        description: "Could not update RCA settings. Please try again.",
        variant: "destructive",
      });
    } finally {
      setUpdatingRca(false);
    }
  };

  const copyWebhookUrl = async () => {
    if (!webhookData?.webhookUrl) return;

    try {
      await navigator.clipboard.writeText(webhookData.webhookUrl);
      toast({
        title: "Copied",
        description: "Webhook URL copied to clipboard",
      });
    } catch (error) {
      console.error("Failed to copy to clipboard:", error);
      toast({
        title: "Copy failed",
        description: "Could not copy to clipboard. Please copy manually.",
        variant: "destructive",
      });
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2 className="h-5 w-5 text-green-500" />
          Connected to Splunk
        </CardTitle>
        <CardDescription>
          Your Splunk instance is connected. Configure webhooks to receive alerts.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Connection Info */}
        <div className="bg-muted/50 rounded-lg p-4 space-y-2">
          <div className="flex justify-between">
            <span className="text-sm text-muted-foreground">Instance URL</span>
            <span className="text-sm font-medium">{status.baseUrl}</span>
          </div>
          {status.server?.name && (
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Server Name</span>
              <span className="text-sm font-medium">{status.server.name}</span>
            </div>
          )}
          {status.server?.version && (
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Version</span>
              <span className="text-sm font-medium">{status.server.version}</span>
            </div>
          )}
          {status.username && (
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Username</span>
              <span className="text-sm font-medium">{status.username}</span>
            </div>
          )}
        </div>

        {/* Quick Actions */}
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/splunk/alerts")}>
            View Alerts
          </Button>
          <Button variant="outline" onClick={() => router.push("/splunk/search")}>
            Search Logs
          </Button>
        </div>

        {/* Alert RCA Toggle */}
        <div className="border-t pt-6">
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label htmlFor="rca-toggle" className="text-base font-medium">
                Enable Alert RCA
              </Label>
              <p className="text-sm text-muted-foreground">
                Automatically investigate Splunk alerts with Aurora
              </p>
            </div>
            {loadingRcaSettings ? (
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            ) : (
              <Switch
                id="rca-toggle"
                checked={rcaEnabled}
                onCheckedChange={handleRcaToggle}
                disabled={updatingRca}
              />
            )}
          </div>
        </div>

        {/* Webhook Configuration - Only show when RCA is enabled */}
        {rcaEnabled && (
          <div className="border-t pt-6">
            <h3 className="font-medium mb-4">Configure Webhook Alert Action</h3>

            {loadingWebhook ? (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading webhook URL...
              </div>
            ) : webhookData ? (
              <div className="space-y-4">
                <div>
                  <label className="text-sm font-medium">Your Webhook URL</label>
                  <div className="flex gap-2 mt-1">
                    <code className="flex-1 p-2 bg-muted rounded text-xs break-all">
                      {webhookData.webhookUrl}
                    </code>
                    <Button variant="outline" size="icon" onClick={copyWebhookUrl}>
                      <Copy className="h-4 w-4" />
                    </Button>
                  </div>
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
                  href="https://docs.splunk.com/Documentation/Splunk/latest/Alert/Webhooks"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-sm text-blue-600 hover:underline"
                >
                  View Splunk Webhook Documentation <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Failed to load webhook URL</p>
            )}
          </div>
        )}

        {/* Disconnect */}
        <div className="border-t pt-6">
          <Button
            variant="destructive"
            onClick={onDisconnect}
            disabled={loading}
            className="w-full"
          >
            {loading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Disconnecting...
              </>
            ) : (
              "Disconnect Splunk"
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

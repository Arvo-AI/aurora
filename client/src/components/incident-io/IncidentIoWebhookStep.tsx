"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { CheckCircle2, Copy, ExternalLink, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { incidentIoService, IncidentIoWebhookUrlResponse, IncidentIoRcaSettings } from "@/lib/services/incident-io";
import { copyToClipboard } from "@/lib/utils";

interface IncidentIoWebhookStepProps {
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

export function IncidentIoWebhookStep({ onDisconnect, loading }: IncidentIoWebhookStepProps) {
  const router = useRouter();
  const { toast } = useToast();
  const [webhookData, setWebhookData] = useState<IncidentIoWebhookUrlResponse | null>(null);
  const [loadingWebhook, setLoadingWebhook] = useState(true);
  const [rcaEnabled, setRcaEnabled] = useState(false);
  const [postbackEnabled, setPostbackEnabled] = useState(false);
  const [loadingSettings, setLoadingSettings] = useState(true);
  const [updatingRca, setUpdatingRca] = useState(false);
  const [updatingPostback, setUpdatingPostback] = useState(false);

  useEffect(() => {
    let isMounted = true;

    const loadData = async () => {
      setLoadingWebhook(true);
      setLoadingSettings(true);

      try {
        const [webhookResponse, rcaSettings] = await Promise.all([
          incidentIoService.getWebhookUrl(),
          incidentIoService.getRcaSettings(),
        ]);

        if (isMounted) {
          setWebhookData(webhookResponse);
          if (rcaSettings) {
            setRcaEnabled(rcaSettings.rcaEnabled);
            setPostbackEnabled(rcaSettings.postbackEnabled);
          }
        }
      } catch (error) {
        console.error("Failed to load incident.io settings:", error);
      } finally {
        if (isMounted) {
          setLoadingWebhook(false);
          setLoadingSettings(false);
        }
      }
    };

    loadData();
    return () => { isMounted = false; };
  }, []);

  const handleRcaToggle = async (enabled: boolean) => {
    setUpdatingRca(true);
    try {
      const result = await incidentIoService.updateRcaSettings({ rcaEnabled: enabled });
      if (result) {
        setRcaEnabled(result.rcaEnabled);
        setPostbackEnabled(result.postbackEnabled);
      }
      toast({
        title: enabled ? "Automatic RCA Enabled" : "Automatic RCA Disabled",
        description: enabled
          ? "Aurora will automatically investigate new incidents from incident.io"
          : "New incidents will be stored but not automatically investigated",
      });
    } catch (error) {
      toast({
        title: "Failed to update settings",
        description: "Could not update RCA settings. Please try again.",
        variant: "destructive",
      });
    } finally {
      setUpdatingRca(false);
    }
  };

  const handlePostbackToggle = async (enabled: boolean) => {
    setUpdatingPostback(true);
    try {
      const result = await incidentIoService.updateRcaSettings({ postbackEnabled: enabled });
      if (result) {
        setPostbackEnabled(result.postbackEnabled);
      }
      toast({
        title: enabled ? "Post-back Enabled" : "Post-back Disabled",
        description: enabled
          ? "RCA results will be posted to the incident.io timeline"
          : "RCA results will only be available in Aurora",
      });
    } catch (error) {
      toast({
        title: "Failed to update settings",
        description: "Could not update post-back setting. Please try again.",
        variant: "destructive",
      });
    } finally {
      setUpdatingPostback(false);
    }
  };

  const copyWebhookUrl = async () => {
    if (!webhookData?.webhookUrl) return;
    try {
      await copyToClipboard(webhookData.webhookUrl);
      toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
    } catch {
      toast({ title: "Copy failed", description: "Could not copy to clipboard.", variant: "destructive" });
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2 className="h-5 w-5 text-green-500" />
          Connected to incident.io
        </CardTitle>
        <CardDescription>
          Your incident.io account is connected. Configure webhooks to receive incident events.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Quick Actions */}
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/incident-io/incidents")}>
            View Incidents
          </Button>
        </div>

        {/* Automatic RCA Toggle */}
        <div className="border-t pt-6">
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label htmlFor="rca-toggle" className="text-base font-medium">
                Automatic RCA
              </Label>
              <p className="text-sm text-muted-foreground">
                Automatically investigate new incidents with Aurora
              </p>
            </div>
            {loadingSettings ? (
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

        {/* Post-back Toggle — only show when RCA is enabled */}
        {rcaEnabled && (
          <div className="border-t pt-6">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="postback-toggle" className="text-base font-medium">
                  Post RCA to incident.io
                </Label>
                <p className="text-sm text-muted-foreground">
                  Automatically post RCA results back to the incident timeline
                </p>
              </div>
              {loadingSettings ? (
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              ) : (
                <Switch
                  id="postback-toggle"
                  checked={postbackEnabled}
                  onCheckedChange={handlePostbackToggle}
                  disabled={updatingPostback}
                />
              )}
            </div>
          </div>
        )}

        {/* Webhook Configuration */}
        <div className="border-t pt-6">
          <h3 className="font-medium mb-4">Webhook Configuration</h3>

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
                href="https://incident.io/docs/webhooks"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-sm text-blue-600 hover:underline"
              >
                View incident.io Webhook Documentation <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              Unable to load webhook URL. Please try refreshing.
            </p>
          )}
        </div>

        {/* Disconnect */}
        <div className="border-t pt-6">
          <Button
            variant="destructive"
            onClick={onDisconnect}
            disabled={loading}
          >
            {loading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Disconnecting...
              </>
            ) : (
              "Disconnect incident.io"
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

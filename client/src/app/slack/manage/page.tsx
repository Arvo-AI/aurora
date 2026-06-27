"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { ArrowLeft, Loader2, LogOut, Bell } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { slackService, type SlackStatus } from "@/lib/services/slack";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite as checkCanWrite } from "@/lib/roles";
import { DisconnectConfirmDialog } from "@/components/ui/disconnect-confirm-dialog";
import { queryClient, jsonFetcher } from "@/lib/query";

const SLACK_NOTIFICATION_KEYS = [
  { key: "slack_investigation_start_notifications", label: "Investigation Started", description: "Notify when Aurora begins an RCA investigation", defaultValue: true },
  { key: "slack_investigation_complete_notifications", label: "Investigation Complete", description: "Notify when Aurora finishes an RCA investigation", defaultValue: true },
  { key: "slack_action_start_notifications", label: "Action Started", description: "Notify when an Aurora Action begins running", defaultValue: true },
  { key: "slack_action_complete_notifications", label: "Action Complete", description: "Notify when an Aurora Action finishes", defaultValue: true },
] as const;

type PreferenceKey = typeof SLACK_NOTIFICATION_KEYS[number]["key"];

export default function SlackManagePage() {
  const router = useRouter();
  const { toast } = useToast();
  const { user } = useUser();
  const canWrite = checkCanWrite(user?.role);

  const [slackStatus, setSlackStatus] = useState<SlackStatus | null>(null);
  const [isLoadingStatus, setIsLoadingStatus] = useState(true);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [showDisconnectDialog, setShowDisconnectDialog] = useState(false);

  const [preferences, setPreferences] = useState<Record<PreferenceKey, boolean>>({
    slack_investigation_start_notifications: true,
    slack_investigation_complete_notifications: true,
    slack_action_start_notifications: true,
    slack_action_complete_notifications: true,
  });
  const [isLoadingPrefs, setIsLoadingPrefs] = useState(true);
  const [savingPrefs, setSavingPrefs] = useState<Record<string, boolean>>({});

  const loadStatus = useCallback(async () => {
    try {
      const status = await slackService.getStatus();
      setSlackStatus(status);
      if (!status?.connected) {
        router.push("/connectors");
      }
    } catch {
      router.push("/connectors");
    } finally {
      setIsLoadingStatus(false);
    }
  }, [router]);

  const loadPreferences = useCallback(async () => {
    try {
      const keys = SLACK_NOTIFICATION_KEYS.map(({ key }) => key);
      const params = new URLSearchParams();
      keys.forEach((k) => params.append("keys", k));

      const response = await fetch(`/api/proxy/user-preferences/batch?${params.toString()}`);
      if (response.ok) {
        const data = await response.json();
        const loaded: Record<string, boolean> = {};
        for (const { key, defaultValue } of SLACK_NOTIFICATION_KEYS) {
          const val = data.preferences?.[key];
          if (val !== null && val !== undefined) {
            loaded[key] = typeof val === "boolean" ? val : val === "true";
          } else {
            loaded[key] = defaultValue;
          }
        }
        setPreferences((prev) => ({ ...prev, ...loaded } as Record<PreferenceKey, boolean>));
      }
    } catch (error) {
      console.error("Error loading Slack notification preferences:", error);
    } finally {
      setIsLoadingPrefs(false);
    }
  }, []);

  useEffect(() => {
    loadStatus();
    loadPreferences();
  }, [loadStatus, loadPreferences]);

  const handlePreferenceChange = async (key: PreferenceKey, enabled: boolean) => {
    setPreferences((prev) => ({ ...prev, [key]: enabled }));
    setSavingPrefs((prev) => ({ ...prev, [key]: true }));

    try {
      const response = await fetch("/api/proxy/user-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, value: enabled }),
      });

      if (!response.ok) {
        throw new Error("Failed to save preference");
      }
    } catch {
      setPreferences((prev) => ({ ...prev, [key]: !enabled }));
      toast({
        title: "Error",
        description: "Failed to save notification preference",
        variant: "destructive",
      });
    } finally {
      setSavingPrefs((prev) => ({ ...prev, [key]: false }));
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      await slackService.disconnect();
      if (globalThis.window !== undefined) {
        localStorage.removeItem("isSlackConnected");
        globalThis.window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
      queryClient.invalidate("/api/connectors/status", jsonFetcher);
      toast({ title: "Success", description: "Slack disconnected successfully" });
      router.push("/connectors");
    } catch (error: any) {
      toast({
        title: "Disconnect Failed",
        description: error.message || "Failed to disconnect Slack",
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
      setShowDisconnectDialog(false);
    }
  };

  if (isLoadingStatus) {
    return (
      <div className="min-h-screen bg-black text-white flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-black text-white p-8">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center gap-4 mb-8">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push("/connectors")}
            className="text-zinc-400 hover:text-white"
          >
            <ArrowLeft className="h-4 w-4 mr-2" />
            Back to Connectors
          </Button>
        </div>

        <div className="flex items-center gap-3 mb-8">
          <div className="p-2 rounded-lg bg-white">
            <img src="/slack.png" alt="Slack" className="h-8 w-8" />
          </div>
          <div>
            <h1 className="text-2xl font-bold">Slack Integration</h1>
            <p className="text-sm text-zinc-400">Manage your Slack connection and notification preferences</p>
          </div>
        </div>

        {/* Connection Info */}
        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="text-lg">Connection</CardTitle>
            <CardDescription>Your current Slack workspace connection</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {slackStatus && (
              <>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted-foreground font-medium w-32">Workspace:</span>
                  {slackStatus.team_url ? (
                    <a
                      href={slackStatus.team_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sm font-semibold text-primary hover:underline"
                    >
                      {slackStatus.team_name || "Slack Workspace"}
                    </a>
                  ) : (
                    <span className="text-sm font-semibold">{slackStatus.team_name || "Slack Workspace"}</span>
                  )}
                </div>
                {slackStatus.team_id && (
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground font-medium w-32">Team ID:</span>
                    <span className="text-sm text-muted-foreground">{slackStatus.team_id}</span>
                  </div>
                )}
                {slackStatus.incidents_channel_name && (
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground font-medium w-32">Channel:</span>
                    <span className="text-sm font-semibold">#{slackStatus.incidents_channel_name}</span>
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>

        {/* Notification Settings */}
        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="text-lg flex items-center gap-2">
              <Bell className="h-5 w-5" />
              Notification Settings
            </CardTitle>
            <CardDescription>
              Configure which events send notifications to your Slack channel
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {SLACK_NOTIFICATION_KEYS.map(({ key, label, description }) => (
              <div key={key} className={`flex items-center justify-between p-4 border rounded-lg ${canWrite ? "" : "opacity-50"}`}>
                <div className="space-y-1 flex-1">
                  <h4 className="font-medium text-sm">{label}</h4>
                  <p className="text-xs text-muted-foreground">{description}</p>
                </div>
                <Switch
                  checked={preferences[key]}
                  onCheckedChange={(checked) => handlePreferenceChange(key, checked)}
                  disabled={isLoadingPrefs || savingPrefs[key] || !canWrite}
                  className="ml-4"
                />
              </div>
            ))}
          </CardContent>
        </Card>

        {/* Danger Zone */}
        <Card className="border-destructive/50">
          <CardHeader>
            <CardTitle className="text-lg text-destructive">Danger Zone</CardTitle>
            <CardDescription>
              Disconnect Slack from Aurora. You will stop receiving all Slack notifications.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              variant="destructive"
              onClick={() => setShowDisconnectDialog(true)}
              disabled={isDisconnecting || !canWrite}
            >
              {isDisconnecting ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Disconnecting...
                </>
              ) : (
                <>
                  <LogOut className="h-4 w-4 mr-2" />
                  Disconnect Slack
                </>
              )}
            </Button>
          </CardContent>
        </Card>
      </div>

      <DisconnectConfirmDialog
        open={showDisconnectDialog}
        onOpenChange={setShowDisconnectDialog}
        connectorName="Slack"
        onConfirm={handleDisconnect}
      />
    </div>
  );
}

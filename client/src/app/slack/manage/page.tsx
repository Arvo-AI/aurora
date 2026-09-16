"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { ArrowLeft, Loader2, LogOut, Bell, Hash, RefreshCw, X, Pencil, ChevronDown, ChevronRight } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { slackService, type SlackStatus, type SlackConnectedChannel } from "@/lib/services/slack";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite as checkCanWrite } from "@/lib/roles";
import { DisconnectConfirmDialog } from "@/components/ui/disconnect-confirm-dialog";
import { queryClient, jsonFetcher } from "@/lib/query";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const SLACK_NOTIFICATION_KEYS = [
  { key: "slack_investigation_start_notifications", label: "Investigation Started", description: "Notify when Aurora begins an RCA investigation", defaultValue: true },
  { key: "slack_investigation_complete_notifications", label: "Investigation Complete", description: "Notify when Aurora finishes an RCA investigation", defaultValue: true },
  { key: "slack_action_start_notifications", label: "Action Started", description: "Notify when an Aurora Action begins running", defaultValue: true },
  { key: "slack_action_complete_notifications", label: "Action Complete", description: "Notify when an Aurora Action finishes", defaultValue: true },
] as const;

type PreferenceKey = typeof SLACK_NOTIFICATION_KEYS[number]["key"];

// Two logical groups, each backed by a start + end boolean preference. The UI
// exposes a single "when to notify" dropdown per group and derives/writes the
// two underlying booleans, so the backend dispatcher gates stay unchanged.
type NotifyMode = "never" | "start" | "end" | "both";

const NOTIFICATION_GROUPS = [
  {
    id: "investigation",
    label: "Investigations",
    description: "When Aurora runs an RCA investigation",
    startKey: "slack_investigation_start_notifications" as PreferenceKey,
    endKey: "slack_investigation_complete_notifications" as PreferenceKey,
  },
  {
    id: "actions",
    label: "Actions",
    description: "When an Aurora Action runs",
    startKey: "slack_action_start_notifications" as PreferenceKey,
    endKey: "slack_action_complete_notifications" as PreferenceKey,
  },
] as const;

const NOTIFY_MODE_LABELS: Record<NotifyMode, string> = {
  never: "Never",
  start: "On start",
  end: "On end",
  both: "On start and end",
};

function modeFromBooleans(start: boolean, end: boolean): NotifyMode {
  if (start && end) return "both";
  if (start) return "start";
  if (end) return "end";
  return "never";
}

function booleansFromMode(mode: NotifyMode): { start: boolean; end: boolean } {
  return { start: mode === "start" || mode === "both", end: mode === "end" || mode === "both" };
}

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

  const [connectedChannels, setConnectedChannels] = useState<SlackConnectedChannel[]>([]);
  const [dismissedChannels, setDismissedChannels] = useState<SlackConnectedChannel[]>([]);
  const [isLoadingChannels, setIsLoadingChannels] = useState(true);
  const [isRefreshingChannels, setIsRefreshingChannels] = useState(false);
  // channel_id currently being edited inline (description pen), plus its draft
  const [editingChannelId, setEditingChannelId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const [showDismissed, setShowDismissed] = useState(false);

  const loadChannels = useCallback(async () => {
    try {
      const data = await slackService.getChannels();
      setConnectedChannels(data.connected);
      setDismissedChannels(data.dismissed);
    } catch (error) {
      console.error("Error loading Slack channels:", error);
    } finally {
      setIsLoadingChannels(false);
    }
  }, []);

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
    loadChannels();
  }, [loadStatus, loadPreferences, loadChannels]);

  const handleDismissChannel = async (channelId: string) => {
    // Optimistically move from active to dismissed.
    const target = connectedChannels.find((c) => c.channel_id === channelId);
    setConnectedChannels((prev) => prev.filter((c) => c.channel_id !== channelId));
    if (target) {
      setDismissedChannels((prev) => [{ ...target, is_dismissed: true, notify_enabled: false }, ...prev]);
    }
    try {
      await slackService.dismissChannel(channelId);
    } catch {
      await loadChannels(); // Reconcile on failure.
      toast({ title: "Error", description: "Failed to dismiss channel", variant: "destructive" });
    }
  };

  const handleRestoreChannel = async (channelId: string) => {
    const target = dismissedChannels.find((c) => c.channel_id === channelId);
    setDismissedChannels((prev) => prev.filter((c) => c.channel_id !== channelId));
    if (target) {
      setConnectedChannels((prev) => [...prev, { ...target, is_dismissed: false }]);
    }
    try {
      await slackService.restoreChannel(channelId);
    } catch {
      await loadChannels();
      toast({ title: "Error", description: "Failed to restore channel", variant: "destructive" });
    }
  };

  const startEditingDescription = (c: SlackConnectedChannel) => {
    setEditingChannelId(c.channel_id);
    setEditingDraft(c.metadata_summary || "");
  };

  const cancelEditingDescription = () => {
    setEditingChannelId(null);
    setEditingDraft("");
  };

  const saveEditedDescription = async (channelId: string) => {
    const summary = editingDraft.trim();
    setConnectedChannels((prev) =>
      prev.map((c) =>
        c.channel_id === channelId ? { ...c, metadata_summary: summary, metadata_status: "ready" } : c,
      ),
    );
    setEditingChannelId(null);
    try {
      await slackService.updateChannelDescription(channelId, summary);
    } catch {
      await loadChannels();
      toast({ title: "Error", description: "Failed to update description", variant: "destructive" });
    }
  };

  // "Generate with AI" from inside the editor: close it and kick off the same
  // LLM generation as the regenerate action (result lands on next load).
  const handleGenerateFromEditor = (channelId: string) => {
    setEditingChannelId(null);
    handleRegenerateDescription(channelId);
  };

  const handleChannelNotifyToggle = async (channelId: string, enabled: boolean) => {
    setConnectedChannels((prev) =>
      prev.map((c) => (c.channel_id === channelId ? { ...c, notify_enabled: enabled } : c)),
    );
    try {
      await slackService.setChannelNotify(channelId, enabled);
    } catch {
      // Revert on failure.
      setConnectedChannels((prev) =>
        prev.map((c) => (c.channel_id === channelId ? { ...c, notify_enabled: !enabled } : c)),
      );
      toast({ title: "Error", description: "Failed to update channel notification setting", variant: "destructive" });
    }
  };

  const handleRegenerateDescription = async (channelId: string) => {
    try {
      await slackService.regenerateChannelDescription(channelId);
      toast({ title: "Regenerating", description: "Channel description is being regenerated." });
      setConnectedChannels((prev) =>
        prev.map((c) => (c.channel_id === channelId ? { ...c, metadata_status: "generating" } : c)),
      );
    } catch {
      toast({ title: "Error", description: "Failed to regenerate description", variant: "destructive" });
    }
  };

  // Re-scan the workspace so already-connected orgs (or ones with new channels)
  // pick up channels without reconnecting. Reloads the list after.
  const handleRefreshChannels = async () => {
    setIsRefreshingChannels(true);
    try {
      const { described } = await slackService.refreshChannels();
      await loadChannels();
      toast({
        title: "Channels refreshed",
        description: described > 0
          ? `Found new channels — generating ${described} description${described === 1 ? "" : "s"}.`
          : "Channel list is up to date.",
      });
    } catch {
      toast({ title: "Error", description: "Failed to refresh channels", variant: "destructive" });
    } finally {
      setIsRefreshingChannels(false);
    }
  };

  const persistPreference = async (key: PreferenceKey, enabled: boolean): Promise<boolean> => {
    const response = await fetch("/api/proxy/user-preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value: enabled }),
    });
    return response.ok;
  };

  const handleGroupModeChange = async (
    group: typeof NOTIFICATION_GROUPS[number],
    mode: NotifyMode,
  ) => {
    const { start, end } = booleansFromMode(mode);
    // Snapshot for rollback if a write fails.
    const prevStart = preferences[group.startKey];
    const prevEnd = preferences[group.endKey];

    setPreferences((prev) => ({ ...prev, [group.startKey]: start, [group.endKey]: end }));
    setSavingPrefs((prev) => ({ ...prev, [group.id]: true }));

    try {
      const [okStart, okEnd] = await Promise.all([
        persistPreference(group.startKey, start),
        persistPreference(group.endKey, end),
      ]);
      if (!okStart || !okEnd) throw new Error("Failed to save preference");
    } catch {
      setPreferences((prev) => ({ ...prev, [group.startKey]: prevStart, [group.endKey]: prevEnd }));
      toast({
        title: "Error",
        description: "Failed to save notification preference",
        variant: "destructive",
      });
    } finally {
      setSavingPrefs((prev) => ({ ...prev, [group.id]: false }));
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
            {NOTIFICATION_GROUPS.map((group) => {
              const mode = modeFromBooleans(preferences[group.startKey], preferences[group.endKey]);
              return (
                <div
                  key={group.id}
                  className={`flex items-center justify-between p-4 border rounded-lg ${canWrite ? "" : "opacity-50"}`}
                >
                  <div className="space-y-1 flex-1">
                    <h4 className="font-medium text-sm">{group.label}</h4>
                    <p className="text-xs text-muted-foreground">{group.description}</p>
                  </div>
                  <Select
                    value={mode}
                    onValueChange={(v) => handleGroupModeChange(group, v as NotifyMode)}
                    disabled={isLoadingPrefs || savingPrefs[group.id] || !canWrite}
                  >
                    <SelectTrigger className="w-44 ml-4">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(["never", "start", "end", "both"] as NotifyMode[]).map((m) => (
                        <SelectItem key={m} value={m}>
                          {NOTIFY_MODE_LABELS[m]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              );
            })}
          </CardContent>
        </Card>

        {/* Channel Awareness */}
        <Card className="mb-6">
          <CardHeader>
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <CardTitle className="text-lg flex items-center gap-2">
                  <Hash className="h-5 w-5" />
                  Channels
                </CardTitle>
                <CardDescription>
                  Aurora is automatically aware of every channel it can see and describes them (all
                  of them when you have 50 or fewer; otherwise the 50 most recent). It uses those
                  descriptions to decide where to post about incidents. Edit a description, generate
                  one on demand, or dismiss channels that aren&apos;t relevant.
                </CardDescription>
              </div>
              {canWrite && (
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  onClick={handleRefreshChannels}
                  disabled={isRefreshingChannels}
                >
                  {isRefreshingChannels ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <RefreshCw className="h-4 w-4 mr-2" />
                  )}
                  Refresh channels
                </Button>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-6">
            {isLoadingChannels ? (
              <div className="flex items-center justify-center py-6">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            ) : (
              <>
                {/* Active channels: description + inline edit, generate, dismiss */}
                {connectedChannels.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    No channels yet. Invite Aurora to channels in Slack, then click Refresh channels.
                  </p>
                ) : (
                  <div className="space-y-3">
                    <h4 className="text-sm font-medium text-muted-foreground">
                      Aurora is aware of ({connectedChannels.length})
                    </h4>
                    {connectedChannels.map((c) => (
                      <div key={c.channel_id} className="p-3 border rounded-lg">
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-2 min-w-0">
                            <span className="text-sm font-semibold truncate">#{c.channel_name || c.channel_id}</span>
                            {c.channel_type && c.channel_type !== "unknown" && (
                              <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300">
                                {c.channel_type}
                              </span>
                            )}
                            {c.detected_platform && (
                              <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-950 text-blue-300">
                                {c.detected_platform}
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-2 shrink-0">
                            <span className="text-xs text-muted-foreground">Notify</span>
                            <Switch
                              checked={Boolean(c.notify_enabled)}
                              onCheckedChange={(checked) => handleChannelNotifyToggle(c.channel_id, checked)}
                              disabled={!canWrite}
                            />
                            {canWrite && (
                              <>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  className="h-7 w-7 p-0 text-zinc-400 hover:text-white"
                                  title="Edit or generate description"
                                  onClick={() => startEditingDescription(c)}
                                >
                                  <Pencil className="h-3.5 w-3.5" />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  className="h-7 w-7 p-0 text-zinc-400 hover:text-destructive"
                                  title="Dismiss (hide from Aurora; stays in Slack)"
                                  onClick={() => handleDismissChannel(c.channel_id)}
                                >
                                  <X className="h-4 w-4" />
                                </Button>
                              </>
                            )}
                          </div>
                        </div>

                        {/* Full description only shows while editing — keeps rows lean. */}
                        {editingChannelId === c.channel_id && (
                          <div className="space-y-2 mt-2">
                            <textarea
                              value={editingDraft}
                              onChange={(e) => setEditingDraft(e.target.value)}
                              rows={3}
                              className="w-full text-xs rounded-md border bg-background p-2"
                              placeholder="Describe what this channel is for and which team/service it serves."
                            />
                            <div className="flex items-center justify-between gap-2">
                              {/* Let AI draft the description in-place instead of typing it. */}
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-6 px-2 text-xs text-zinc-400 hover:text-white"
                                onClick={() => handleGenerateFromEditor(c.channel_id)}
                              >
                                <RefreshCw className="h-3 w-3 mr-1" />
                                {c.metadata_summary ? "Regenerate with AI" : "Generate with AI"}
                              </Button>
                              <div className="flex gap-2">
                                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={cancelEditingDescription}>
                                  Cancel
                                </Button>
                                <Button size="sm" className="h-6 px-2 text-xs" onClick={() => saveEditedDescription(c.channel_id)}>
                                  Save
                                </Button>
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* Dismissed channels: collapsible, with restore */}
                {dismissedChannels.length > 0 && (
                  <div className="space-y-2">
                    <button
                      type="button"
                      className="text-sm font-medium text-muted-foreground hover:text-foreground flex items-center gap-1"
                      onClick={() => setShowDismissed((s) => !s)}
                    >
                      {showDismissed ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                      Dismissed channels ({dismissedChannels.length})
                    </button>
                    {showDismissed && (
                      <div className="border rounded-lg divide-y divide-zinc-800">
                        {dismissedChannels.map((c) => (
                          <div key={c.channel_id} className="flex items-center justify-between gap-2 p-3">
                            <span className="text-sm text-muted-foreground truncate">
                              #{c.channel_name || c.channel_id}
                            </span>
                            {canWrite && (
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-6 px-2 text-xs text-zinc-400 hover:text-white shrink-0"
                                onClick={() => handleRestoreChannel(c.channel_id)}
                              >
                                Restore
                              </Button>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
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

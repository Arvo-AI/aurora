"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { ArrowLeft, Loader2, LogOut, Bell, Hash, RefreshCw, X, Pencil, ChevronDown, ChevronRight, Search, Plus, ChevronsUpDown, Check } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { slackService, type SlackStatus, type SlackConnectedChannel } from "@/lib/services/slack";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite as checkCanWrite } from "@/lib/roles";
import { DisconnectConfirmDialog } from "@/components/ui/disconnect-confirm-dialog";
import { queryClient, jsonFetcher } from "@/lib/query";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

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
    description: "Status cards in your incidents channel when Aurora runs an RCA",
    startKey: "slack_investigation_start_notifications" as PreferenceKey,
    endKey: "slack_investigation_complete_notifications" as PreferenceKey,
  },
  {
    id: "actions",
    label: "Actions",
    description: "Status cards in your incidents channel when an Aurora Action runs",
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
  // The single channel that receives the structured incident card.
  const [cardChannelId, setCardChannelId] = useState<string | null>(null);
  const [isSettingCard, setIsSettingCard] = useState(false);
  // Searchable card-channel picker (Popover open state + its filter query).
  const [cardPickerOpen, setCardPickerOpen] = useState(false);
  const [cardPickerQuery, setCardPickerQuery] = useState("");
  const [isLoadingChannels, setIsLoadingChannels] = useState(true);
  // channel_id currently being edited inline (description pen), plus its draft
  const [editingChannelId, setEditingChannelId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  // Search box over the active channel list (mirrors the activate panel search).
  const [activeQuery, setActiveQuery] = useState("");
  // "Activate more channels" panel: search query, checked ids, in-flight flag.
  // Open by default so the aware-but-inactive channels are discoverable.
  const [showActivate, setShowActivate] = useState(true);
  const [activateQuery, setActivateQuery] = useState("");
  const [activateSelected, setActivateSelected] = useState<Set<string>>(new Set());
  const [isActivating, setIsActivating] = useState(false);

  const loadChannels = useCallback(async () => {
    try {
      const data = await slackService.getChannels();
      setConnectedChannels(data.connected);
      setDismissedChannels(data.dismissed);
      setCardChannelId(data.card_channel_id ?? null);
      // The OAuth callback can redirect while descriptions are still generating.
      // If the initial load shows any pending/generating rows, start polling so
      // the spinner resolves without needing a manual refresh.
      if (data.connected.some((c) => c.metadata_status === "generating" || c.metadata_status === "pending")) {
        pollChannelsUntilSettled();
      }
    } catch (error) {
      console.error("Error loading Slack channels:", error);
    } finally {
      setIsLoadingChannels(false);
    }
  }, []);

  // Description generation runs async on the worker, so poll the channel list
  // until nothing is still 'generating'/'pending' (capped so we never poll
  // forever). Cleared on unmount.
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (pollTimerRef.current) clearTimeout(pollTimerRef.current); }, []);

  const pollChannelsUntilSettled = useCallback((
    attempt = 0,
    keepGoingWhileEmpty = false,
    awaitingIds: string[] = [],
  ) => {
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    // ~30s ceiling (15 tries × 2s) — generation is normally a few seconds.
    if (attempt >= 15) return;
    pollTimerRef.current = setTimeout(async () => {
      try {
        // Poll mode: DB-only read (no Slack). We're only watching metadata_status
        // settle as the worker describes channels, so this must not fire the
        // rate-limited workspace re-list on every 2s tick. It also means the
        // response's `dismissed` (available-to-join) list is empty by design, so
        // we deliberately do NOT refetch dismissedChannels here.
        const data = await slackService.getChannels(true);
        setConnectedChannels(data.connected);
        setCardChannelId(data.card_channel_id ?? null);
        // A channel that just became active (e.g. bulk-activate joined it) must
        // drop out of the Inactive list, otherwise it shows in both places and
        // can be "activated" again. Poll mode doesn't refetch `dismissed`, so
        // prune it locally against the fresh connected set.
        const connectedIds = new Set(data.connected.map((c) => c.channel_id));
        setDismissedChannels((prev) => prev.filter((c) => !connectedIds.has(c.channel_id)));

        const stillWorking = data.connected.some(
          (c) => c.metadata_status === "generating" || c.metadata_status === "pending",
        );
        // keepGoingWhileEmpty: right after connect the background registration
        // task may not have inserted any rows yet, so an empty list isn't "done"
        // — keep polling until rows appear (then normal settle logic takes over).
        const waitingForFirstRows = keepGoingWhileEmpty && data.connected.length === 0;
        // Bulk-activate returns BEFORE the worker inserts the joined rows, so an
        // absence of pending rows isn't "done" yet — keep polling until every
        // submitted id has actually shown up in the connected list.
        const waitingForQueuedIds = awaitingIds.some((id) => !connectedIds.has(id));
        if (stillWorking || waitingForFirstRows || waitingForQueuedIds) {
          pollChannelsUntilSettled(attempt + 1, keepGoingWhileEmpty, awaitingIds);
        }
      } catch (error) {
        console.error("Error polling Slack channels:", error);
      }
    }, 2000);
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
    // Just connected: channel auto-registration runs on the worker now, so the
    // table may be empty for a moment. Poll so rows + descriptions appear
    // without a manual refresh.
    if (globalThis.window !== undefined &&
        new URLSearchParams(globalThis.window.location.search).get("slack_auth") === "success") {
      pollChannelsUntilSettled(0, true);
    }
  }, [loadStatus, loadPreferences, loadChannels, pollChannelsUntilSettled]);

  const handleDismissChannel = async (channelId: string) => {
    const isCard = channelId === cardChannelId;
    // Deactivating the card channel is allowed, but warn: the card then has no
    // destination until the user picks a new one.
    if (isCard && globalThis.window !== undefined &&
        !globalThis.window.confirm(
          "This is your incident card channel. Deactivating it means the incident card won't be posted to any channel until you pick a new one. Continue?",
        )) {
      return;
    }
    // Optimistically move from active to dismissed (and clear the card
    // designation locally if this was the card channel — the backend clears it too).
    const target = connectedChannels.find((c) => c.channel_id === channelId);
    setConnectedChannels((prev) => prev.filter((c) => c.channel_id !== channelId));
    if (target) {
      setDismissedChannels((prev) => [{ ...target, is_dismissed: true }, ...prev]);
    }
    if (isCard) setCardChannelId(null);
    try {
      await slackService.dismissChannel(channelId);
    } catch {
      await loadChannels(); // Reconcile on failure.
      toast({ title: "Error", description: "Failed to deactivate channel", variant: "destructive" });
    }
  };

  const handleSetCardChannel = async (channelId: string) => {
    const prev = cardChannelId;
    setCardChannelId(channelId); // optimistic
    setIsSettingCard(true);
    setCardPickerOpen(false);
    setCardPickerQuery("");
    try {
      await slackService.setCardChannel(channelId);
    } catch {
      setCardChannelId(prev);
      toast({
        title: "Error",
        description: "Failed to set the incident card channel. It must be an active channel.",
        variant: "destructive",
      });
    } finally {
      setIsSettingCard(false);
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

  const handleRegenerateDescription = async (channelId: string) => {
    try {
      await slackService.regenerateChannelDescription(channelId);
      toast({ title: "Regenerating", description: "Channel description is being regenerated." });
      setConnectedChannels((prev) =>
        prev.map((c) => (c.channel_id === channelId ? { ...c, metadata_status: "generating" } : c)),
      );
      // Poll until the worker finishes so the spinner clears without a manual refresh.
      pollChannelsUntilSettled();
    } catch {
      toast({ title: "Error", description: "Failed to regenerate description", variant: "destructive" });
    }
  };

  // Activate the checked indexed channels: describe them + make them routable.
  const handleActivateSelected = async () => {
    const ids = Array.from(activateSelected);
    if (ids.length === 0) return;
    setIsActivating(true);
    // Optimistically flip to 'generating' so they jump into the active list.
    setConnectedChannels((prev) =>
      prev.map((c) => (activateSelected.has(c.channel_id) ? { ...c, metadata_status: "generating" } : c)),
    );
    try {
      const { queued } = await slackService.activateChannels(ids);
      // Remove only the ids we just submitted — preserve any selections the user
      // made while the request was in flight.
      setActivateSelected((prev) => {
        const requested = new Set(ids);
        return new Set([...prev].filter((id) => !requested.has(id)));
      });
      setActivateQuery("");
      // Bulk-activate returns before the worker inserts the joined rows, so tell
      // the poller to keep going until each submitted id actually appears in the
      // connected list (not just until pending rows clear).
      pollChannelsUntilSettled(0, false, ids);
      toast({
        title: "Activating channels",
        description: `Joining and describing ${queued} channel${queued === 1 ? "" : "s"}.`,
      });
    } catch {
      await loadChannels(); // Reconcile optimistic flip on failure.
      toast({ title: "Error", description: "Failed to activate channels", variant: "destructive" });
    } finally {
      setIsActivating(false);
    }
  };

  const toggleActivateSelected = (channelId: string) => {
    setActivateSelected((prev) => {
      const next = new Set(prev);
      // Toggle membership so the same click both selects and deselects.
      if (next.has(channelId)) next.delete(channelId);
      else next.add(channelId);
      return next;
    });
  };

  // Select/deselect every channel currently matching the search. Operates on the
  // filtered matches (passed in) so "Select all" after searching "oncall" grabs
  // exactly those, not the whole index.
  const toggleSelectAllMatches = (matchIds: string[], allSelected: boolean) => {
    setActivateSelected((prev) => {
      const next = new Set(prev);
      // All matches already selected → this acts as "Clear" for the matches.
      if (allSelected) matchIds.forEach((id) => next.delete(id));
      else matchIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const persistPreference = async (key: PreferenceKey, enabled: boolean): Promise<boolean> => {
    try {
      const response = await fetch("/api/proxy/user-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, value: enabled }),
      });
      return response.ok;
    } catch {
      // Network/transport failure — treat as a failed write so the caller can
      // roll back this key specifically (rather than throwing and losing the
      // per-key success info from the other concurrent write).
      return false;
    }
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
      // One dropdown maps to two prefs (start + end), so this is two writes.
      const [okStart, okEnd] = await Promise.all([
        persistPreference(group.startKey, start),
        persistPreference(group.endKey, end),
      ]);
      if (!okStart || !okEnd) {
        // Partial success: only roll back the key(s) whose write actually
        // failed — the succeeded one is already persisted on the backend, so
        // reverting it in the UI would desync the two.
        setPreferences((prev) => ({
          ...prev,
          ...(okStart ? {} : { [group.startKey]: prevStart }),
          ...(okEnd ? {} : { [group.endKey]: prevEnd }),
        }));
        throw new Error("Failed to save preference");
      }
    } catch {
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

  // Split the active set (described / being described — the channels Aurora
  // actually engages) from the aware-only index ('skipped'). The main list shows
  // only the active set; indexed channels live under "Activate more channels".
  // The card channel (⭐) is pinned to the top so it's always visible even when
  // the list is long/scrolled; the rest keep their existing (recency) order.
  const activeChannels = connectedChannels
    .filter((c) => c.metadata_status !== "skipped")
    .sort((a, b) => {
      if (a.channel_id === cardChannelId) return -1;
      if (b.channel_id === cardChannelId) return 1;
      return 0;
    });
  const activeQ = activeQuery.trim().toLowerCase();
  const activeMatches = activeQ
    ? activeChannels.filter((c) => (c.channel_name || c.channel_id).toLowerCase().includes(activeQ))
    : activeChannels;
  // Inactive = everything the user can promote to Active: aware-only channels
  // (never activated, 'skipped', which arrive in `connected`) AND channels the
  // user deactivated (`dismissed`, a separate array from the API). Both are
  // silent to Aurora and share one "Inactive" section — activating either
  // restores + describes it.
  const inactiveChannels = [
    ...connectedChannels.filter((c) => c.metadata_status === "skipped"),
    ...dismissedChannels,
  ];
  // Display name for the currently-selected card channel (used in the picker
  // trigger). Falls back to the id if we don't have a name row.
  const cardChannel = activeChannels.find((c) => c.channel_id === cardChannelId);
  const cardChannelName = cardChannel?.channel_name || (cardChannelId ? cardChannelId : "");
  // Card picker only offers ACTIVE channels (the card must go to one Aurora
  // engages), filtered by the picker's search box.
  const cardPickerQ = cardPickerQuery.trim().toLowerCase();
  const cardPickerMatches = cardPickerQ
    ? activeChannels.filter((c) => (c.channel_name || c.channel_id).toLowerCase().includes(cardPickerQ))
    : activeChannels;
  // Only surface the card-channel picker when the card is actually posted for
  // something — if both event groups are "never", there's nothing to route.
  const anyCardEventEnabled = NOTIFICATION_GROUPS.some(
    (g) => modeFromBooleans(preferences[g.startKey], preferences[g.endKey]) !== "never",
  );
  const activateQ = activateQuery.trim().toLowerCase();
  const activateMatches = activateQ
    ? inactiveChannels.filter((c) => (c.channel_name || c.channel_id).toLowerCase().includes(activateQ))
    : inactiveChannels;

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

        {/* Incident Card & Notifications */}
        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="text-lg flex items-center gap-2">
              <Bell className="h-5 w-5" />
              Incident Card
            </CardTitle>
            <CardDescription>
              The incident card is the structured banner Aurora posts (and keeps
              updated) for an investigation — alert, severity, service, root
              cause, links. It goes to <span className="font-medium">one</span>{" "}
              channel. This does not affect Aurora replying when @mentioned, or
              posting an investigation&apos;s conclusion to relevant team
              channels — that routing is always on and tuned in Aurora&apos;s
              Slack memory.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Per-event card behaviour: never / on start / on end / both. */}
            <div>
              <h4 className="text-sm font-medium mb-1">When to post the card</h4>
              <p className="text-xs text-muted-foreground mb-3">
                Choose when the card is posted to the card channel for investigations and actions.
              </p>
              <div className="space-y-4">
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
              </div>
            </div>

            {/* Card channel picker — only relevant once the card is posted for
                at least one event (both "never" ⇒ nothing to route, so hide it). */}
            {anyCardEventEnabled && (
              <div className="flex items-center justify-between gap-4 p-4 border rounded-lg">
                <div className="space-y-1 flex-1 min-w-0">
                  <h4 className="font-medium text-sm">Card channel</h4>
                  <p className="text-xs text-muted-foreground">
                    Where Aurora posts the structured incident card. Must be an active channel.
                  </p>
                </div>
                <Popover open={cardPickerOpen} onOpenChange={setCardPickerOpen}>
                  <PopoverTrigger asChild>
                    <Button
                      variant="outline"
                      role="combobox"
                      aria-expanded={cardPickerOpen}
                      className="w-56 justify-between shrink-0"
                      disabled={!canWrite || isSettingCard || activeChannels.length === 0}
                    >
                      <span className="truncate">
                        {cardChannelName ? `#${cardChannelName}` : "Select a channel…"}
                      </span>
                      {isSettingCard
                        ? <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                        : <ChevronsUpDown className="h-4 w-4 shrink-0 opacity-50" />}
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent className="w-56 p-0" align="end">
                    <div className="relative border-b">
                      <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                      <input
                        autoFocus
                        value={cardPickerQuery}
                        onChange={(e) => setCardPickerQuery(e.target.value)}
                        placeholder="Search channels…"
                        className="w-full bg-transparent py-2 pl-9 pr-2 text-sm outline-none"
                      />
                    </div>
                    <div className="max-h-56 overflow-y-auto py-1">
                      {cardPickerMatches.length === 0 ? (
                        <p className="px-3 py-2 text-xs text-muted-foreground">No channels match.</p>
                      ) : cardPickerMatches.map((c) => (
                        <button
                          key={c.channel_id}
                          type="button"
                          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-accent"
                          onClick={() => handleSetCardChannel(c.channel_id)}
                        >
                          <Check className={`h-4 w-4 shrink-0 ${c.channel_id === cardChannelId ? "opacity-100" : "opacity-0"}`} />
                          <span className="truncate">#{c.channel_name || c.channel_id}</span>
                        </button>
                      ))}
                    </div>
                  </PopoverContent>
                </Popover>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Channel Awareness */}
        <Card className="mb-6">
          <CardHeader>
            <div className="min-w-0">
              <CardTitle className="text-lg flex items-center gap-2">
                <Hash className="h-5 w-5" />
                Channels
              </CardTitle>
              <CardDescription>
                Aurora is automatically aware of every channel it can see, and writes a
                description for the ones it&apos;s been invited to — so it engages where it&apos;s a
                member, like a teammate. It uses those descriptions to decide where to post about
                incidents. For any other channel, generate a description on demand to make it
                routable, edit a description, or dismiss channels that aren&apos;t relevant.
              </CardDescription>
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
                {activeChannels.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    No active channels yet. Invite Aurora to channels in Slack, or activate channels
                    below.
                  </p>
                ) : (
                  <div className="space-y-3">
                    <div className="flex items-center gap-2">
                      <span className="h-2 w-2 rounded-full bg-emerald-500" />
                      <h3 className="text-base font-semibold text-foreground">
                        Active channels
                        <span className="ml-1.5 text-sm font-normal text-muted-foreground">({activeChannels.length})</span>
                      </h3>
                    </div>
                    <p className="text-xs text-muted-foreground -mt-1">
                      Aurora posts free-form teammate messages in these channels when relevant.
                      The structured incident card goes to the card channel set in the Incident Card section above.
                    </p>
                    {/* Search over the active list, so a long membership stays
                        manageable (mirrors the activate panel's search). */}
                    {activeChannels.length > 5 && (
                      <div className="relative">
                        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                        <Input
                          value={activeQuery}
                          onChange={(e) => setActiveQuery(e.target.value)}
                          placeholder="Search active channels by name"
                          className="pl-9 text-sm"
                        />
                      </div>
                    )}
                    {/* Scrollable so a large active set doesn't stretch the page;
                        the section keeps a fixed max height and scrolls inside. */}
                    <div className="max-h-96 overflow-y-auto space-y-3 pr-1">
                    {activeMatches.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No active channels match.</p>
                    ) : activeMatches.map((c) => (
                      <div key={c.channel_id} className="p-2 rounded-md border border-border space-y-1">
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
                          <div className="flex items-center gap-1 shrink-0">
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
                                  title={c.channel_id === cardChannelId
                                    ? "Deactivate (this is the card channel — the card will stop posting until you pick a new one)"
                                    : "Deactivate (Aurora stays in Slack but stops posting here)"}
                                  onClick={() => handleDismissChannel(c.channel_id)}
                                >
                                  <X className="h-4 w-4" />
                                </Button>
                              </>
                            )}
                          </div>
                        </div>

                        {/* Description shows inline like the GitHub repos list —
                            clamped to 2 lines so long, multi-line summaries don't
                            blow up the row. Editing (below) reveals the full text. */}
                        {editingChannelId !== c.channel_id && (
                          <div className="text-xs">
                            {c.metadata_status === "generating" || c.metadata_status === "pending" ? (
                              <span className="flex items-center gap-1.5 text-muted-foreground">
                                <Loader2 className="h-3 w-3 animate-spin" />
                                Generating description…
                              </span>
                            ) : c.metadata_status === "error" ? (
                              <span className="text-red-400">Couldn&apos;t generate a description — edit to add one.</span>
                            ) : c.metadata_summary ? (
                              <p className="text-muted-foreground line-clamp-2">
                                {c.metadata_summary.replace(/\*\*/g, "")}
                              </p>
                            ) : (
                              <span className="text-muted-foreground/70 italic">No description yet.</span>
                            )}
                          </div>
                        )}

                        {/* Full description only shows while editing — keeps rows lean. */}
                        {editingChannelId === c.channel_id && (
                          <div className="space-y-2">
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
                  </div>
                )}

                {/* Inactive channels: aware-only index + user-deactivated
                    channels, together. Search, pick, and activate to promote
                    any of them to Active (describe + route). */}
                {canWrite && inactiveChannels.length > 0 && (
                  <div className="space-y-2 pt-4 mt-4 border-t border-border">
                    <button
                      type="button"
                      className="group flex items-center gap-2 w-full text-left"
                      onClick={() => setShowActivate((s) => !s)}
                    >
                      {showActivate ? <ChevronDown className="h-4 w-4 text-muted-foreground" /> : <ChevronRight className="h-4 w-4 text-muted-foreground" />}
                      <span className="h-2 w-2 rounded-full bg-zinc-500" />
                      <h3 className="text-base font-semibold text-foreground group-hover:text-white">
                        Inactive channels
                        <span className="ml-1.5 text-sm font-normal text-muted-foreground">({inactiveChannels.length} aware, not active)</span>
                      </h3>
                    </button>
                    {showActivate && (
                      <div className="space-y-3 rounded-lg border p-3">
                        <p className="text-xs text-muted-foreground">
                          Channels Aurora is aware of but doesn&apos;t post to — either not activated
                          yet, or deactivated. Search, pick the ones you want (e.g. all your{" "}
                          <span className="font-mono">oncall</span> channels), and activate — Aurora
                          will describe them and start posting there.
                        </p>
                        <div className="relative">
                          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                          <Input
                            value={activateQuery}
                            onChange={(e) => setActivateQuery(e.target.value)}
                            placeholder="Search channels by name (e.g. oncall, payments, alerts)"
                            className="pl-9 text-sm"
                          />
                        </div>
                        {activateMatches.length > 0 && (() => {
                          const matchIds = activateMatches.map((c) => c.channel_id);
                          const allSelected = matchIds.every((id) => activateSelected.has(id));
                          return (
                            <div className="flex items-center gap-2 px-1">
                              <Checkbox
                                id="activate-select-all"
                                checked={allSelected}
                                onCheckedChange={() => toggleSelectAllMatches(matchIds, allSelected)}
                              />
                              <label htmlFor="activate-select-all" className="text-xs text-muted-foreground cursor-pointer">
                                {allSelected
                                  ? `Clear all ${matchIds.length}`
                                  : `Select all ${matchIds.length}${activateQuery.trim() ? " matching" : ""}`}
                              </label>
                            </div>
                          );
                        })()}
                        <div className="max-h-64 overflow-y-auto rounded-md border divide-y divide-zinc-800">
                          {activateMatches.length === 0 ? (
                            <p className="p-3 text-xs text-muted-foreground">No channels match.</p>
                          ) : (
                            activateMatches.map((c) => (
                              <label
                                key={c.channel_id}
                                htmlFor={`activate-${c.channel_id}`}
                                className="flex items-center gap-2 p-2 cursor-pointer hover:bg-zinc-900/50"
                              >
                                <Checkbox
                                  id={`activate-${c.channel_id}`}
                                  checked={activateSelected.has(c.channel_id)}
                                  onCheckedChange={() => toggleActivateSelected(c.channel_id)}
                                />
                                <span className="text-sm truncate">#{c.channel_name || c.channel_id}</span>
                                {c.channel_type && c.channel_type !== "unknown" && (
                                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300 ml-auto shrink-0">
                                    {c.channel_type}
                                  </span>
                                )}
                              </label>
                            ))
                          )}
                        </div>
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs text-muted-foreground">
                            {activateSelected.size} selected
                          </span>
                          <Button
                            size="sm"
                            className="h-7"
                            disabled={activateSelected.size === 0 || isActivating}
                            onClick={handleActivateSelected}
                          >
                            {isActivating ? (
                              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                            ) : (
                              <Plus className="h-3.5 w-3.5 mr-1.5" />
                            )}
                            Activate selected
                          </Button>
                        </div>
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

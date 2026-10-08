"use client";

import type { ComponentType } from "react";
import { Hash, MessagesSquare } from "lucide-react";
import { SlackMemoryCard } from "@/components/SlackMemoryCard";
import { TeamsMemoryCard } from "@/components/TeamsMemoryCard";
import { slackService, type SlackConnectedChannel, type SlackStatus } from "@/lib/services/slack";
import { teamsService, type TeamsConnectedChannel, type TeamsStatus } from "@/lib/services/teams";

export type ChatPlatformChannel = SlackConnectedChannel | TeamsConnectedChannel;
export type ChatPlatformStatus = SlackStatus | TeamsStatus;

export type NotificationPreferenceKey =
  | `slack_${string}`
  | `teams_${string}`;

export interface ChatPlatformManageConfig {
  id: "slack" | "teams";
  displayName: string;
  connectorName: string;
  logoSrc: string;
  connectedStorageKey: string;
  channelIcon: typeof Hash;
  notificationGroups: readonly {
    id: string;
    label: string;
    description: string;
    startKey: NotificationPreferenceKey;
    endKey: NotificationPreferenceKey;
  }[];
  defaultPreferences: Record<NotificationPreferenceKey, boolean>;
  MemoryCard: ComponentType<{ canWrite: boolean }>;
  service: {
    getStatus: () => Promise<ChatPlatformStatus | null>;
    disconnect: () => Promise<void>;
    getChannels: (pollMode?: boolean) => Promise<{
      connected: ChatPlatformChannel[];
      dismissed: ChatPlatformChannel[];
      card_channel_id?: string | null;
    }>;
    dismissChannel: (channelId: string) => Promise<void>;
    restoreChannel: (channelId: string, teamId?: string) => Promise<void>;
    updateChannelDescription: (channelId: string, summary: string) => Promise<void>;
    setCardChannel: (channelId: string) => Promise<void>;
    regenerateChannelDescription: (channelId: string) => Promise<void>;
    activateChannels: (channelIds: string[]) => Promise<{ queued: number }>;
  };
  copy: {
    activateHint: string;
    emptyActive: string;
    routingNote: string;
    disconnectBlurb: string;
  };
}

export const slackManageConfig: ChatPlatformManageConfig = {
  id: "slack",
  displayName: "Slack",
  connectorName: "Slack",
  logoSrc: "/slack.png",
  connectedStorageKey: "isSlackConnected",
  channelIcon: Hash,
  notificationGroups: [
    {
      id: "investigation",
      label: "Investigations",
      description: "Status cards in your incidents channel when Aurora runs an RCA",
      startKey: "slack_investigation_start_notifications",
      endKey: "slack_investigation_complete_notifications",
    },
    {
      id: "actions",
      label: "Actions",
      description: "Status cards in your incidents channel when an Aurora Action runs",
      startKey: "slack_action_start_notifications",
      endKey: "slack_action_complete_notifications",
    },
  ],
  defaultPreferences: {
    slack_investigation_start_notifications: true,
    slack_investigation_complete_notifications: true,
    slack_action_start_notifications: true,
    slack_action_complete_notifications: true,
  },
  MemoryCard: SlackMemoryCard,
  service: slackService,
  copy: {
    activateHint: "Invite Aurora to channels in Slack, or activate channels below.",
    emptyActive: "No active channels yet. Invite Aurora to channels in Slack, or activate channels from the list below.",
    routingNote: "that routing is always on and tuned in the Slack memory",
    disconnectBlurb: "Disconnect Slack from Aurora. You will stop receiving all Slack notifications.",
  },
};

export const teamsManageConfig: ChatPlatformManageConfig = {
  id: "teams",
  displayName: "Microsoft Teams",
  connectorName: "Microsoft Teams",
  logoSrc: "/microsoft-teams.svg",
  connectedStorageKey: "isTeamsConnected",
  channelIcon: MessagesSquare,
  notificationGroups: [
    {
      id: "investigation",
      label: "Investigations",
      description: "Status cards in your incidents channel when Aurora runs an RCA",
      startKey: "teams_investigation_start_notifications",
      endKey: "teams_investigation_complete_notifications",
    },
    {
      id: "actions",
      label: "Actions",
      description: "Status cards in your incidents channel when an Aurora Action runs",
      startKey: "teams_action_start_notifications",
      endKey: "teams_action_complete_notifications",
    },
  ],
  defaultPreferences: {
    teams_investigation_start_notifications: true,
    teams_investigation_complete_notifications: true,
    teams_action_start_notifications: true,
    teams_action_complete_notifications: true,
  },
  MemoryCard: TeamsMemoryCard,
  service: teamsService,
  copy: {
    activateHint:
      "Install the Aurora Teams app in your team, @mention the bot in channels, then activate channels below.",
    emptyActive:
      "No active channels yet. Install the Teams app, @mention Aurora in a channel, or activate channels from the list below.",
    routingNote: "that routing is always on and tuned in the Teams memory",
    disconnectBlurb: "Disconnect Microsoft Teams from Aurora. You will stop receiving all Teams notifications.",
  },
};

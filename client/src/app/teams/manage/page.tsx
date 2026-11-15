"use client";

import { ChatPlatformManagePage } from "@/components/chat-platform/ChatPlatformManagePage";
import { teamsManageConfig } from "@/lib/chat-platform/manage-config";

export default function TeamsManagePage() {
  return <ChatPlatformManagePage config={teamsManageConfig} />;
}

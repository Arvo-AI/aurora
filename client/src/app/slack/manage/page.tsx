"use client";

import { ChatPlatformManagePage } from "@/components/chat-platform/ChatPlatformManagePage";
import { slackManageConfig } from "@/lib/chat-platform/manage-config";

export default function SlackManagePage() {
  return <ChatPlatformManagePage config={slackManageConfig} />;
}

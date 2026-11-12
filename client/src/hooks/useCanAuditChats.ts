"use client";

import { jsonFetcher, queryClient, useQuery } from "@/lib/query";

export const CHAT_AUDIT_KEY = "/api/org/chat-audit";

export interface ChatAuditSetting {
  /** Org setting: false = admins only (default), true = admins and editors. */
  editors: boolean;
  /** Whether the signed-in user may open the organization's chat history under that setting. */
  allowed: boolean;
}

/**
 * Whether the signed-in user may open Monitor → Chat history (and see the
 * shortcuts to it). The backend decides and enforces; this only hides UI.
 * False until known, and on any error.
 */
export function useCanAuditChats(): boolean {
  const { data } = useQuery<ChatAuditSetting>(CHAT_AUDIT_KEY, jsonFetcher, { staleTime: 60_000 });
  return data?.allowed === true;
}

/** Refetch after the setting changes, so every place that shows the tab updates. */
export function refreshChatAudit(): Promise<ChatAuditSetting> {
  return queryClient.invalidate<ChatAuditSetting>(CHAT_AUDIT_KEY, jsonFetcher);
}

import type React from "react";
import {
  Brain,
  BookOpen,
  Server,
  Lightbulb,
  ScrollText,
  Package,
} from "lucide-react";

export const MEMORY_CATEGORIES = [
  "context",
  "runbook",
  "infrastructure",
  "learned",
  "postmortem",
  "artifact",
] as const;

// Categories users can manually create/upload and filter by — excludes artifact (internal system category)
export const USER_WRITABLE_CATEGORIES = ["context", "runbook", "infrastructure", "learned", "postmortem"] as const;

export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];

export const CATEGORY_META: Record<MemoryCategory, { label: string; icon: React.ReactNode; color: string }> = {
  context: { label: "Context", icon: <Brain className="h-3.5 w-3.5" />, color: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200" },
  runbook: { label: "Runbook", icon: <BookOpen className="h-3.5 w-3.5" />, color: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" },
  infrastructure: { label: "Infrastructure", icon: <Server className="h-3.5 w-3.5" />, color: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" },
  learned: { label: "Learned", icon: <Lightbulb className="h-3.5 w-3.5" />, color: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200" },
  postmortem: { label: "Postmortem", icon: <ScrollText className="h-3.5 w-3.5" />, color: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200" },
  artifact: { label: "Artifact", icon: <Package className="h-3.5 w-3.5" />, color: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200" },
};

export interface MemoryEntry {
  id: string;
  title: string;
  category: MemoryCategory;
  description: string | null;
  last_edited_by: string | null;
  // Actual display name of the person who last edited (e.g. "Olivier").
  // Null for agent edits or pre-migration entries — fall back to last_edited_by.
  last_edited_by_name?: string | null;
  updated_at: string | null;
}

/**
 * Human-readable "by <who>" label for a memory entry's last editor.
 * Prefers the real person's name, falling back to the generic "User"/"Agent".
 */
export function formatEditedBy(entry: Pick<MemoryEntry, "last_edited_by" | "last_edited_by_name">): string | null {
  // Real person's name recorded — show it (e.g. "Olivier").
  if (entry.last_edited_by_name && entry.last_edited_by_name.trim()) {
    return entry.last_edited_by_name.trim();
  }
  // No name — fall back to the generic actor label ("user"/"agent"), capitalized.
  if (entry.last_edited_by) {
    return entry.last_edited_by.charAt(0).toUpperCase() + entry.last_edited_by.slice(1);
  }
  return null;
}

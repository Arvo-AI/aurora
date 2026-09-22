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

// The system-maintained category — users may view these entries but not edit/delete them.
export const SYSTEM_CATEGORY = "artifact";

export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];

export const CATEGORY_META: Record<MemoryCategory, { label: string; icon: React.ReactNode; color: string }> = {
  context: { label: "Context", icon: <Brain className="h-3.5 w-3.5" />, color: "bg-purple-500/10 text-purple-700 dark:text-purple-400 border-purple-500/20" },
  runbook: { label: "Runbook", icon: <BookOpen className="h-3.5 w-3.5" />, color: "bg-blue-500/10 text-blue-700 dark:text-blue-400 border-blue-500/20" },
  infrastructure: { label: "Infrastructure", icon: <Server className="h-3.5 w-3.5" />, color: "bg-green-500/10 text-green-700 dark:text-green-400 border-green-500/20" },
  learned: { label: "Learned", icon: <Lightbulb className="h-3.5 w-3.5" />, color: "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border-yellow-500/20" },
  postmortem: { label: "Postmortem", icon: <ScrollText className="h-3.5 w-3.5" />, color: "bg-red-500/10 text-red-700 dark:text-red-400 border-red-500/20" },
  artifact: { label: "Artifact", icon: <Package className="h-3.5 w-3.5" />, color: "bg-zinc-500/10 text-zinc-700 dark:text-zinc-300 border-zinc-500/20" },
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

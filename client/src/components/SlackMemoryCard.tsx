"use client";

import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Brain, Loader2, Pencil, RotateCw } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { MarkdownRenderer } from "@/components/ui/markdown-renderer";
import { formatEditedBy, type MemoryEntry } from "@/lib/memory-constants";

// The Slack memory is an ordinary `context` / "Slack" memory entry. It's mirrored
// here so Slack behaviour is editable alongside the rest of the Slack settings
// instead of only being findable in the full memory list.
interface SlackMemory extends MemoryEntry {
  content: string;
}

interface SlackMemoryCardProps {
  // Editing is gated on the same role check the rest of the manage page uses.
  canWrite: boolean;
}

export function SlackMemoryCard({ canWrite }: SlackMemoryCardProps) {
  const { toast } = useToast();

  const [memory, setMemory] = useState<SlackMemory | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  // Inline editor state: the pencil swaps the rendered view for a raw markdown draft.
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  const loadMemory = useCallback(async () => {
    setIsLoading(true);
    setLoadFailed(false);
    try {
      const res = await fetch("/api/proxy/memory/slack", { cache: "no-store" });
      if (!res.ok) throw new Error("Failed to load Slack memory");
      const data = await res.json();
      setMemory(data.entry ?? null);
    } catch (error) {
      console.error("Error loading Slack memory:", error);
      setLoadFailed(true);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadMemory();
  }, [loadMemory]);

  const startEditing = () => {
    if (!memory) return;
    setDraft(memory.content || "");
    setIsEditing(true);
  };

  const handleSave = async () => {
    if (!memory) return;
    const content = draft.trim();
    if (!content) {
      toast({ title: "Nothing to save", description: "Slack memory can't be empty.", variant: "destructive" });
      return;
    }

    setIsSaving(true);
    try {
      // Send content only — title/category are the well-known identity the agent's
      // prompt injector pins to, so they must never change from this card.
      const res = await fetch(`/api/proxy/memory/entries/${memory.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      if (!res.ok) {
        const text = await res.text();
        let msg = "Failed to save Slack memory";
        try { msg = JSON.parse(text).error || msg; } catch {}
        throw new Error(msg);
      }
      setIsEditing(false);
      // Refetch so the "last updated by" line reflects this edit.
      await loadMemory();
      toast({ title: "Slack memory updated" });
    } catch (error) {
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : "Failed to save Slack memory",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return "";
    return new Date(dateStr).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  };

  const editedBy = memory ? formatEditedBy(memory) : null;

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="text-lg flex items-center gap-2">
          <Brain className="h-5 w-5" />
          Slack Memory
        </CardTitle>
        <CardDescription>
          How Aurora behaves in Slack — its tone, when it speaks up, the message
          format, and which teams/channels own which services. Aurora reads this
          before every Slack reply and appends to it as your team states
          preferences, so editing it here changes how Aurora shows up in Slack.
          This is the same <span className="font-medium">Context → Slack</span>{" "}
          memory entry from Settings → Memory, surfaced here so you don&apos;t have
          to go looking for it.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex items-center justify-center py-6">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : loadFailed || !memory ? (
          /* GET seeds on demand, so a null entry means the load or seed failed. */
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">
              Couldn&apos;t load the Slack memory.
            </p>
            <Button variant="outline" size="sm" className="h-7" onClick={loadMemory}>
              <RotateCw className="h-3.5 w-3.5 mr-1.5" />
              Retry
            </Button>
          </div>
        ) : isEditing ? (
          <div className="space-y-2">
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              className="min-h-[320px] font-mono text-xs"
              placeholder="Describe how Aurora should behave in Slack (markdown)."
            />
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                Markdown. Aurora also edits this itself as it learns — your changes are versioned.
              </p>
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  onClick={() => setIsEditing(false)}
                  disabled={isSaving}
                >
                  Cancel
                </Button>
                <Button size="sm" className="h-7 px-2 text-xs" onClick={handleSave} disabled={isSaving}>
                  {isSaving ? <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" /> : null}
                  Save
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            {/* Scrollable so a long, well-tuned policy doesn't stretch the page. */}
            <div className="max-h-96 overflow-y-auto rounded-lg border p-4">
              {memory.content?.trim() ? (
                <MarkdownRenderer content={memory.content} />
              ) : (
                <p className="text-xs text-muted-foreground/70 italic">
                  This memory is empty — Aurora has no Slack-specific guidance yet.
                </p>
              )}
            </div>
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                {memory.updated_at ? `Last updated ${formatDate(memory.updated_at)}` : ""}
                {editedBy ? ` by ${editedBy}` : ""}
              </p>
              {canWrite && (
                <Button variant="outline" size="sm" className="h-7" onClick={startEditing}>
                  <Pencil className="h-3.5 w-3.5 mr-1.5" />
                  Edit
                </Button>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

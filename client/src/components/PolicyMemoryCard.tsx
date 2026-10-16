"use client";

import { useState, useEffect, useCallback, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Brain, Loader2, Pencil, RotateCw } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { MarkdownRenderer } from "@/components/ui/markdown-renderer";
import { formatEditedBy, type MemoryEntry } from "@/lib/memory-constants";

// A well-known policy memory entry (e.g. `context` / "Slack"), mirrored on its
// connector page so it's editable in context instead of only in the memory list.
interface PolicyMemory extends MemoryEntry {
  content: string;
}

interface PolicyMemoryCardProps {
  // Editing is gated on the same role check the rest of the page uses.
  readonly canWrite: boolean;
  // Backend path under /api/proxy/memory/, e.g. "slack".
  readonly endpoint: string;
  // Integration name used in copy, e.g. "Slack".
  readonly name: string;
  readonly description: ReactNode;
  readonly placeholder: string;
  readonly className?: string;
}

export function PolicyMemoryCard({
  canWrite,
  endpoint,
  name,
  description,
  placeholder,
  className = "mb-6",
}: PolicyMemoryCardProps) {
  const { toast } = useToast();

  const [memory, setMemory] = useState<PolicyMemory | null>(null);
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
      const res = await fetch(`/api/proxy/memory/${endpoint}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`Failed to load ${name} memory`);
      const data = await res.json();
      setMemory(data.entry ?? null);
    } catch (error) {
      console.error(`Error loading ${name} memory:`, error);
      setLoadFailed(true);
    } finally {
      setIsLoading(false);
    }
  }, [endpoint, name]);

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
      toast({ title: "Nothing to save", description: `${name} memory can't be empty.`, variant: "destructive" });
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
        let msg = `Failed to save ${name} memory`;
        try { msg = JSON.parse(text).error || msg; } catch {}
        throw new Error(msg);
      }
      setIsEditing(false);
      // Refetch so the "last updated by" line reflects this edit.
      await loadMemory();
      toast({ title: `${name} memory updated` });
    } catch (error) {
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : `Failed to save ${name} memory`,
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

  // Split the four view states into named renderers rather than one nested
  // ternary chain — keeps each branch readable and the component simple.
  const renderLoading = () => (
    <div className="flex items-center justify-center py-6">
      <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
    </div>
  );

  // GET seeds on demand, so a null entry means the load or the seed failed.
  const renderLoadError = () => (
    <div className="flex items-center justify-between gap-3">
      <p className="text-xs text-muted-foreground">Couldn&apos;t load the {name} memory.</p>
      <Button variant="outline" size="sm" className="h-7" onClick={loadMemory}>
        <RotateCw className="h-3.5 w-3.5 mr-1.5" />
        Retry
      </Button>
    </div>
  );

  const renderEditor = () => (
    <div className="space-y-2">
      <Textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        className="min-h-[320px] font-mono text-xs"
        placeholder={placeholder}
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
  );

  const renderPolicy = (entry: PolicyMemory) => (
    <div className="space-y-2">
      {/* Scrollable so a long, well-tuned policy doesn't stretch the page. */}
      <div className="max-h-96 overflow-y-auto rounded-lg border p-4">
        {entry.content?.trim() ? (
          <MarkdownRenderer content={entry.content} />
        ) : (
          <p className="text-xs text-muted-foreground/70 italic">
            This memory is empty — Aurora has no {name}-specific guidance yet.
          </p>
        )}
      </div>
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {entry.updated_at ? `Last updated ${formatDate(entry.updated_at)}` : ""}
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
  );

  const renderContent = () => {
    if (isLoading) return renderLoading();
    if (loadFailed || !memory) return renderLoadError();
    if (isEditing) return renderEditor();
    return renderPolicy(memory);
  };

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="text-lg flex items-center gap-2">
          <Brain className="h-5 w-5" />
          {name} Memory
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>{renderContent()}</CardContent>
    </Card>
  );
}

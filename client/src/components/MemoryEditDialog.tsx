"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import {
  type MemoryCategory,
  type MemoryEntry,
  CATEGORY_META,
  USER_WRITABLE_CATEGORIES,
} from "@/lib/memory-constants";

interface MemoryEditDialogProps {
  // The entry being edited, or null when the dialog is closed.
  entry: MemoryEntry | null;
  onOpenChange: (open: boolean) => void;
  // Called after a successful save so the parent can refresh its list.
  onSaved: () => void | Promise<void>;
  // View-only mode for system-managed entries: same content fetch, but all
  // fields are disabled and there's no Save button.
  readOnly?: boolean;
}

export function MemoryEditDialog({ entry, onOpenChange, onSaved, readOnly = false }: MemoryEditDialogProps) {
  const { toast } = useToast();

  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<MemoryCategory>("context");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [isLoadingContent, setIsLoadingContent] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  // When a new entry is opened, seed the form from the list row, then fetch
  // the full content (the list endpoint omits content).
  useEffect(() => {
    // Dialog closed — nothing to load.
    if (!entry) return;

    let cancelled = false;
    setTitle(entry.title);
    setCategory(entry.category);
    setDescription(entry.description || "");
    setContent("");
    setIsLoadingContent(true);

    (async () => {
      try {
        const res = await fetch(`/api/proxy/memory/entries/${entry.id}`);
        if (!res.ok) {
          const text = await res.text();
          let msg = "Failed to load memory content";
          try { msg = JSON.parse(text).error || msg; } catch {}
          throw new Error(msg);
        }
        const data = await res.json();
        // A newer open may have superseded this fetch — ignore stale results.
        if (cancelled) return;
        setContent(data.content || "");
        setTitle(data.title ?? entry.title);
        setCategory((data.category as MemoryCategory) ?? entry.category);
        setDescription(data.description || "");
      } catch (error) {
        if (cancelled) return;
        toast({
          title: "Failed to load entry",
          description: error instanceof Error ? error.message : "An error occurred",
          variant: "destructive",
        });
        onOpenChange(false);
      } finally {
        if (!cancelled) setIsLoadingContent(false);
      }
    })();

    return () => { cancelled = true; };
    // Re-run only when the target entry changes (by id).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry?.id]);

  const handleSave = async () => {
    if (!entry) return;

    setIsSaving(true);
    try {
      const res = await fetch(`/api/proxy/memory/entries/${entry.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title.trim(),
          category,
          description: description.trim(),
          content,
        }),
      });

      if (res.ok) {
        toast({ title: "Memory entry updated" });
        onOpenChange(false);
        await onSaved();
      } else {
        const text = await res.text();
        let msg = "Failed to update entry";
        try { msg = JSON.parse(text).error || msg; } catch {}
        throw new Error(msg);
      }
    } catch (error) {
      toast({
        title: "Failed to update",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <Dialog open={!!entry} onOpenChange={(open) => { if (!open) onOpenChange(false); }}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{readOnly ? "View Memory Entry" : "Edit Memory Entry"}</DialogTitle>
          <DialogDescription>
            {readOnly
              ? "This entry is maintained by Aurora and is read-only. You can inspect its content here."
              : "Review and modify this entry's content. Changes are versioned."}
          </DialogDescription>
        </DialogHeader>

        {isLoadingContent ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <label htmlFor="edit-memory-title" className="text-sm font-medium">Title</label>
                <Input
                  id="edit-memory-title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Title"
                  disabled={readOnly}
                />
              </div>
              <div className="space-y-1">
                <label htmlFor="edit-memory-category" className="text-sm font-medium">Category</label>
                {readOnly ? (
                  // System entries use the 'artifact' category, which isn't in the
                  // writable list — render a static label instead of an empty Select.
                  <Input
                    id="edit-memory-category"
                    value={CATEGORY_META[category]?.label ?? category}
                    disabled
                  />
                ) : (
                  <Select value={category} onValueChange={(v) => setCategory(v as MemoryCategory)}>
                    <SelectTrigger id="edit-memory-category">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {USER_WRITABLE_CATEGORIES.map((cat) => (
                        <SelectItem key={cat} value={cat}>
                          {CATEGORY_META[cat].label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </div>
            </div>
            <div className="space-y-1">
              <label htmlFor="edit-memory-description" className="text-sm font-medium">Description (optional)</label>
              <Input
                id="edit-memory-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Brief summary of what this contains"
                disabled={readOnly}
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="edit-memory-content" className="text-sm font-medium">Content (Markdown)</label>
              <Textarea
                id="edit-memory-content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                className="min-h-[300px] font-mono text-sm"
                disabled={readOnly}
              />
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={isSaving}>
            {readOnly ? "Close" : "Cancel"}
          </Button>
          {!readOnly && (
            <Button
              onClick={handleSave}
              disabled={isSaving || isLoadingContent || !title.trim() || !content.trim()}
            >
              {isSaving ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Saving...
                </>
              ) : (
                "Save Changes"
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

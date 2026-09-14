"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
import { useToast } from "@/hooks/use-toast";
import { useUserId } from "@/hooks/use-user-id";
import {
  Loader2,
  Upload,
  Trash2,
  FileText,
  Brain,
  Plus,
  Pencil,
} from "lucide-react";
import { useUser } from "@/hooks/useAuthHooks";
import { DiscoverySettings } from "@/components/DiscoverySettings";
import { MemoryEditDialog } from "@/components/MemoryEditDialog";
import { canWrite as checkCanWrite } from "@/lib/roles";
import {
  type MemoryCategory,
  type MemoryEntry,
  USER_WRITABLE_CATEGORIES,
  CATEGORY_META,
  formatEditedBy,
} from "@/lib/memory-constants";

export function MemorySettings() {
  const { userId, isLoading: userLoading } = useUserId();
  const { user } = useUser();
  const canWrite = checkCanWrite(user?.role);
  const { toast } = useToast();

  // Memory entries state
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [isLoadingEntries, setIsLoadingEntries] = useState(true);
  const [filterCategory, setFilterCategory] = useState<string>("all");
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Create form state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newCategory, setNewCategory] = useState<MemoryCategory>("context");
  const [newContent, setNewContent] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  // Holds the attempted create payload when a same-title+category entry already
  // exists, so we can prompt the user to overwrite or keep both.
  const [createConflict, setCreateConflict] = useState<{
    category: MemoryCategory;
    title: string;
    content: string;
    description?: string;
  } | null>(null);

  // Upload state
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Edit dialog state — the entry currently being edited (dialog logic lives in MemoryEditDialog).
  const [editingEntry, setEditingEntry] = useState<MemoryEntry | null>(null);

  const fetchEntries = useCallback(async () => {
    if (!userId) {
      setIsLoadingEntries(false);
      return;
    }

    try {
      const url = filterCategory === "all"
        ? "/api/proxy/memory/entries"
        : `/api/proxy/memory/entries?category=${filterCategory}`;
      const res = await fetch(url);

      if (res.ok) {
        const data = await res.json();
        setEntries(data.entries || []);
      }
    } catch (error) {
      console.error("Failed to fetch memory entries:", error);
    } finally {
      setIsLoadingEntries(false);
    }
  }, [userId, filterCategory]);

  useEffect(() => {
    if (userId && !userLoading) {
      fetchEntries();
    }
  }, [userId, userLoading, fetchEntries]);

  // Core create request. Returns the Response so callers can branch on conflicts.
  const submitCreate = async (payload: {
    category: MemoryCategory;
    title: string;
    content: string;
    description?: string;
    overwrite?: boolean;
  }) => {
    return fetch("/api/proxy/memory/entries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  };

  // Reset the create form and close it after a successful create.
  const resetCreateForm = () => {
    setNewTitle("");
    setNewCategory("context");
    setNewContent("");
    setNewDescription("");
    setShowCreateForm(false);
  };

  // Build a unique "keep both" title by appending (2), (3), ... within the same
  // category, so the new entry doesn't collide with the existing one.
  const buildUniqueTitle = (title: string, category: MemoryCategory) => {
    const takenInCategory = new Set(
      entries
        .filter((e) => e.category === category)
        .map((e) => e.title.toLowerCase())
    );
    let n = 2;
    let candidate = `${title} (${n})`;
    while (takenInCategory.has(candidate.toLowerCase())) {
      n += 1;
      candidate = `${title} (${n})`;
    }
    return candidate;
  };

  const handleCreate = async () => {
    if (!userId) return;

    const payload = {
      category: newCategory,
      title: newTitle.trim(),
      content: newContent.trim(),
      description: newDescription.trim() || undefined,
    };

    setIsCreating(true);
    try {
      const res = await submitCreate(payload);

      if (res.ok) {
        toast({ title: "Memory entry created" });
        resetCreateForm();
        await fetchEntries();
        return;
      }

      // Same title+category already exists — prompt the user to choose.
      if (res.status === 409) {
        setCreateConflict(payload);
        return;
      }

      const text = await res.text();
      let msg = "Failed to create entry";
      try { msg = JSON.parse(text).error || msg; } catch {}
      throw new Error(msg);
    } catch (error) {
      toast({
        title: "Failed to create",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setIsCreating(false);
    }
  };

  // Resolve a create conflict by either overwriting the existing entry or
  // creating a second entry under a unique "(2)" title.
  const resolveConflict = async (mode: "overwrite" | "keep-both") => {
    if (!createConflict) return;

    const payload =
      mode === "overwrite"
        ? { ...createConflict, overwrite: true }
        : { ...createConflict, title: buildUniqueTitle(createConflict.title, createConflict.category) };

    setIsCreating(true);
    try {
      const res = await submitCreate(payload);
      if (res.ok) {
        toast({
          title: mode === "overwrite" ? "Memory entry overwritten" : "Memory entry created",
        });
        setCreateConflict(null);
        resetCreateForm();
        await fetchEntries();
      } else {
        const text = await res.text();
        let msg = "Failed to save entry";
        try { msg = JSON.parse(text).error || msg; } catch {}
        throw new Error(msg);
      }
    } catch (error) {
      toast({
        title: "Failed to save",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setIsCreating(false);
    }
  };

  const handleDelete = async (entryId: string, title: string) => {
    if (!userId) return;

    setDeletingId(entryId);
    try {
      const res = await fetch(`/api/proxy/memory/entries/${entryId}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const text = await res.text();
        let msg = "Failed to delete entry";
        try { msg = JSON.parse(text).error || msg; } catch {}
        throw new Error(msg);
      }

      toast({
        title: "Entry deleted",
        description: `"${title}" has been removed.`,
      });
      await fetchEntries();
    } catch (error) {
      toast({
        title: "Delete failed",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setDeletingId(null);
    }
  };

  const handleUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0 || !userId) return;

    const allowedTypes = [".md", ".txt", ".pdf"];

    // Validate all files first
    for (const file of Array.from(files)) {
      const dotIndex = file.name.lastIndexOf(".");
      if (dotIndex === -1 || dotIndex === file.name.length - 1) {
        toast({
          title: "Invalid file type",
          description: `"${file.name}" is not supported. Use: .md, .txt, .pdf`,
          variant: "destructive",
        });
        return;
      }
      const ext = file.name.toLowerCase().slice(dotIndex);
      if (!allowedTypes.includes(ext)) {
        toast({
          title: "Invalid file type",
          description: `"${file.name}" is not supported. Use: .md, .txt, .pdf`,
          variant: "destructive",
        });
        return;
      }
      if (file.size > 50 * 1024 * 1024) {
        toast({
          title: "File too large",
          description: `"${file.name}" exceeds 50MB limit`,
          variant: "destructive",
        });
        return;
      }
    }

    setIsUploading(true);
    let successCount = 0;
    let lastError: string | null = null;

    for (const file of Array.from(files)) {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("category", "context");

      try {
        const res = await fetch("/api/proxy/memory/upload", {
          method: "POST",
          body: formData,
        });

        if (res.ok) {
          successCount++;
        } else {
          const text = await res.text();
          try { lastError = JSON.parse(text).error || `Failed to upload "${file.name}"`; } catch { lastError = `Failed to upload "${file.name}"`; }
        }
      } catch (error) {
        lastError = error instanceof Error ? error.message : `Failed to upload "${file.name}"`;
      }
    }

    if (successCount > 0) {
      toast({
        title: successCount === 1 ? "File uploaded" : `${successCount} files uploaded`,
        description: successCount === 1
          ? `"${files[0].name}" has been added to memory.`
          : `${successCount} file(s) have been added to memory.`,
      });
      await fetchEntries();
    }
    if (lastError && successCount < files.length) {
      toast({
        title: "Some uploads failed",
        description: lastError,
        variant: "destructive",
      });
    }

    setIsUploading(false);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return "";
    const d = new Date(dateStr);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  };

  const handleCategoryChange = async (entryId: string, newCategory: MemoryCategory) => {
    try {
      const res = await fetch(`/api/proxy/memory/entries/${entryId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ category: newCategory }),
      });
      if (res.ok) {
        setEntries((prev) =>
          prev.map((e) => e.id === entryId ? { ...e, category: newCategory } : e)
        );
      } else {
        const text = await res.text();
        let msg = "An error occurred";
        try { msg = JSON.parse(text).error || msg; } catch {}
        toast({
          title: "Failed to update category",
          description: msg,
          variant: "destructive",
        });
      }
    } catch {
      toast({
        title: "Failed to update category",
        description: "An error occurred",
        variant: "destructive",
      });
    }
  };

  if (userLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const filteredEntries = entries;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Memory</h1>
        <p className="text-muted-foreground">
          Manage your team&apos;s knowledge — context, runbooks, infrastructure docs, and learnings Aurora references during investigations.
        </p>
      </div>

      {!canWrite && (
        <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/30 px-4 py-3">
          <p className="text-sm text-blue-800 dark:text-blue-200">
            You have read-only access. Contact an admin to get Editor or Admin role to manage memory.
          </p>
        </div>
      )}

      {/* Memory Entries Section */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Brain className="h-5 w-5 text-primary" />
              <CardTitle>Memory Entries</CardTitle>
            </div>
            <div className="flex items-center gap-2">
              <Select value={filterCategory} onValueChange={(v) => setFilterCategory(v)}>
                <SelectTrigger className="w-[150px]">
                  <SelectValue placeholder="All categories" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All categories</SelectItem>
                  {USER_WRITABLE_CATEGORIES.map((cat) => (
                    <SelectItem key={cat} value={cat}>
                      {CATEGORY_META[cat].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {canWrite && (
                <>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".md,.txt,.pdf"
                    multiple
                    onChange={handleUpload}
                    className="hidden"
                    id="memory-upload"
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={isUploading}
                  >
                    {isUploading ? (
                      <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                    ) : (
                      <Upload className="h-4 w-4 mr-1" />
                    )}
                    {isUploading ? "Uploading..." : "Upload"}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => setShowCreateForm(!showCreateForm)}>
                    <Plus className="h-4 w-4 mr-1" />
                    New
                  </Button>
                </>
              )}
            </div>
          </div>
          <CardDescription>
            All knowledge Aurora accumulates — manually added context, uploaded runbooks, discovered infrastructure, and learned patterns.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Create Form */}
          {showCreateForm && canWrite && (
            <div className="border rounded-lg p-4 space-y-3 bg-muted/30">
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label htmlFor="memory-title" className="text-sm font-medium">Title</label>
                  <Input
                    id="memory-title"
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                    placeholder="e.g. Production Runbook"
                  />
                </div>
                <div className="space-y-1">
                  <label htmlFor="memory-category" className="text-sm font-medium">Category</label>
                  <Select value={newCategory} onValueChange={(v) => setNewCategory(v as MemoryCategory)}>
                    <SelectTrigger id="memory-category">
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
                </div>
              </div>
              <div className="space-y-1">
                <label htmlFor="memory-description" className="text-sm font-medium">Description (optional)</label>
                <Input
                  id="memory-description"
                  value={newDescription}
                  onChange={(e) => setNewDescription(e.target.value)}
                  placeholder="Brief summary of what this contains"
                />
              </div>
              <div className="space-y-1">
                <label htmlFor="memory-content" className="text-sm font-medium">Content (Markdown)</label>
                <Textarea
                  id="memory-content"
                  value={newContent}
                  onChange={(e) => setNewContent(e.target.value)}
                  placeholder="## Incident Response Runbook&#10;&#10;1. Check service health..."
                  className="min-h-[150px] font-mono text-sm"
                />
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setShowCreateForm(false)}>
                  Cancel
                </Button>
                <Button
                  onClick={handleCreate}
                  disabled={isCreating || !newTitle.trim() || !newContent.trim()}
                >
                  {isCreating ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Creating...
                    </>
                  ) : (
                    "Create Entry"
                  )}
                </Button>
              </div>
            </div>
          )}


          {/* Entry List */}
          {isLoadingEntries ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : filteredEntries.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center">
              <FileText className="mx-auto h-12 w-12 text-muted-foreground/50" />
              <p className="mt-2 text-sm text-muted-foreground">
                {filterCategory === "all"
                  ? "No memory entries yet. Create one or upload a file to get started."
                  : `No entries in the "${CATEGORY_META[filterCategory as MemoryCategory]?.label}" category.`}
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {filteredEntries.map((entry) => {
                const meta = CATEGORY_META[entry.category] || CATEGORY_META.artifact;
                return (
                  <div
                    key={entry.id}
                    className="flex items-center justify-between rounded-lg border p-3"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="flex-shrink-0">
                        {meta.icon}
                      </div>
                      <div className="min-w-0">
                        <p className="font-medium truncate">{entry.title}</p>
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          {canWrite ? (
                            <Select
                              value={entry.category}
                              onValueChange={(v) => handleCategoryChange(entry.id, v as MemoryCategory)}
                            >
                              <SelectTrigger className={`h-5 w-auto gap-1 border-0 px-1.5 py-0 text-xs ${meta.color}`}>
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
                          ) : (
                            <Badge variant="secondary" className={`text-xs px-1.5 py-0 ${meta.color}`}>
                              {meta.label}
                            </Badge>
                          )}
                          {entry.description && (
                            <span className="truncate max-w-[200px]">{entry.description}</span>
                          )}
                          {entry.updated_at && (
                            <span>{formatDate(entry.updated_at)}</span>
                          )}
                          {formatEditedBy(entry) && (
                            <span>by {formatEditedBy(entry)}</span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {canWrite && (
                        <>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => setEditingEntry(entry)}
                            title="Edit memory entry"
                          >
                            <Pencil className="h-4 w-4 text-muted-foreground hover:text-primary" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleDelete(entry.id, entry.title)}
                            disabled={deletingId === entry.id}
                            title="Delete memory entry"
                          >
                            {deletingId === entry.id ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
                            )}
                          </Button>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Edit Memory Dialog */}
      <MemoryEditDialog
        entry={editingEntry}
        onOpenChange={(open) => { if (!open) setEditingEntry(null); }}
        onSaved={fetchEntries}
      />

      {/* Duplicate title conflict — let the user overwrite or keep both */}
      <Dialog
        open={!!createConflict}
        onOpenChange={(open) => { if (!open && !isCreating) setCreateConflict(null); }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Entry already exists</DialogTitle>
            <DialogDescription>
              {createConflict && (
                <>
                  A memory entry titled &ldquo;{createConflict.title}&rdquo; already exists in the{" "}
                  {CATEGORY_META[createConflict.category]?.label ?? createConflict.category} category.
                  Overwrite it, or keep both by saving this as a new entry?
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-2">
            <Button variant="ghost" onClick={() => setCreateConflict(null)} disabled={isCreating}>
              Cancel
            </Button>
            <Button
              variant="outline"
              onClick={() => resolveConflict("keep-both")}
              disabled={isCreating}
            >
              {isCreating ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              Keep both
            </Button>
            <Button
              variant="destructive"
              onClick={() => resolveConflict("overwrite")}
              disabled={isCreating}
            >
              {isCreating ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              Overwrite
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DiscoverySettings />
    </div>
  );
}

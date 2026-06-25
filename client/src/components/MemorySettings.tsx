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
import { useToast } from "@/hooks/use-toast";
import { useUserId } from "@/hooks/use-user-id";
import {
  Loader2,
  Upload,
  Trash2,
  FileText,
  Brain,
  Plus,
  BookOpen,
  Server,
  Lightbulb,
  ScrollText,
  Package,
} from "lucide-react";
import { useUser } from "@/hooks/useAuthHooks";
import { DiscoverySettings } from "@/components/DiscoverySettings";
import { canWrite as checkCanWrite } from "@/lib/roles";

const MEMORY_CATEGORIES = [
  "context",
  "runbook",
  "infrastructure",
  "learned",
  "postmortem",
  "artifact",
] as const;

type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];

const CATEGORY_META: Record<MemoryCategory, { label: string; icon: React.ReactNode; color: string }> = {
  context: { label: "Context", icon: <Brain className="h-3.5 w-3.5" />, color: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200" },
  runbook: { label: "Runbook", icon: <BookOpen className="h-3.5 w-3.5" />, color: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" },
  infrastructure: { label: "Infrastructure", icon: <Server className="h-3.5 w-3.5" />, color: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" },
  learned: { label: "Learned", icon: <Lightbulb className="h-3.5 w-3.5" />, color: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200" },
  postmortem: { label: "Postmortem", icon: <ScrollText className="h-3.5 w-3.5" />, color: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200" },
  artifact: { label: "Artifact", icon: <Package className="h-3.5 w-3.5" />, color: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200" },
};

interface MemoryEntry {
  id: string;
  title: string;
  category: MemoryCategory;
  description: string | null;
  last_edited_by: string | null;
  updated_at: string | null;
}

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

  // Upload state
  const [isUploading, setIsUploading] = useState(false);
  const [uploadCategory, setUploadCategory] = useState<MemoryCategory>("runbook");
  const fileInputRef = useRef<HTMLInputElement>(null);

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

  const handleCreate = async () => {
    if (!userId) return;

    setIsCreating(true);
    try {
      const res = await fetch("/api/proxy/memory/entries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category: newCategory,
          title: newTitle.trim(),
          content: newContent.trim(),
          description: newDescription.trim() || undefined,
        }),
      });

      if (res.ok) {
        toast({ title: "Memory entry created" });
        setNewTitle("");
        setNewCategory("context");
        setNewContent("");
        setNewDescription("");
        setShowCreateForm(false);
        await fetchEntries();
      } else {
        const data = await res.json();
        throw new Error(data.error || "Failed to create entry");
      }
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

  const handleDelete = async (entryId: string, title: string) => {
    if (!userId) return;

    setDeletingId(entryId);
    try {
      const res = await fetch(`/api/proxy/memory/entries/${entryId}`, {
        method: "DELETE",
      });

      if (res.ok) {
        toast({
          title: "Entry deleted",
          description: `"${title}" has been removed.`,
        });
        await fetchEntries();
      } else {
        const data = await res.json();
        throw new Error(data.error || "Failed to delete entry");
      }
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
    const file = event.target.files?.[0];
    if (!file || !userId) return;

    const allowedTypes = [".md", ".txt", ".pdf"];
    const dotIndex = file.name.lastIndexOf(".");
    if (dotIndex === -1 || dotIndex === file.name.length - 1) {
      toast({
        title: "Invalid file type",
        description: "Supported formats: Markdown (.md), Plain Text (.txt), PDF (.pdf)",
        variant: "destructive",
      });
      return;
    }
    const ext = file.name.toLowerCase().slice(dotIndex);
    if (!allowedTypes.includes(ext)) {
      toast({
        title: "Invalid file type",
        description: "Supported formats: Markdown (.md), Plain Text (.txt), PDF (.pdf)",
        variant: "destructive",
      });
      return;
    }

    if (file.size > 50 * 1024 * 1024) {
      toast({
        title: "File too large",
        description: "Maximum file size is 50MB",
        variant: "destructive",
      });
      return;
    }

    setIsUploading(true);
    const formData = new FormData();
    formData.append("file", file);
    formData.append("category", uploadCategory);

    try {
      const res = await fetch("/api/proxy/memory/upload", {
        method: "POST",
        body: formData,
      });

      if (res.ok) {
        toast({
          title: "File uploaded",
          description: `"${file.name}" has been added to memory.`,
        });
        await fetchEntries();
      } else {
        const data = await res.json();
        throw new Error(data.error || "Failed to upload file");
      }
    } catch (error) {
      toast({
        title: "Upload failed",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return "";
    const d = new Date(dateStr);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
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
                  {MEMORY_CATEGORIES.map((cat) => (
                    <SelectItem key={cat} value={cat}>
                      {CATEGORY_META[cat].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {canWrite && (
                <Button variant="outline" size="sm" onClick={() => setShowCreateForm(!showCreateForm)}>
                  <Plus className="h-4 w-4 mr-1" />
                  New
                </Button>
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
                  <label className="text-sm font-medium">Title</label>
                  <Input
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                    placeholder="e.g. Production Runbook"
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-sm font-medium">Category</label>
                  <Select value={newCategory} onValueChange={(v) => setNewCategory(v as MemoryCategory)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {MEMORY_CATEGORIES.filter((c) => c !== "artifact").map((cat) => (
                        <SelectItem key={cat} value={cat}>
                          {CATEGORY_META[cat].label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div className="space-y-1">
                <label className="text-sm font-medium">Description (optional)</label>
                <Input
                  value={newDescription}
                  onChange={(e) => setNewDescription(e.target.value)}
                  placeholder="Brief summary of what this contains"
                />
              </div>
              <div className="space-y-1">
                <label className="text-sm font-medium">Content (Markdown)</label>
                <Textarea
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

          {/* Upload */}
          {canWrite && (
            <div className="flex items-center gap-3">
              <input
                ref={fileInputRef}
                type="file"
                accept=".md,.txt,.pdf"
                onChange={handleUpload}
                className="hidden"
                id="memory-upload"
              />
              <Select value={uploadCategory} onValueChange={(v) => setUploadCategory(v as MemoryCategory)}>
                <SelectTrigger className="w-[150px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MEMORY_CATEGORIES.filter((c) => c !== "artifact").map((cat) => (
                    <SelectItem key={cat} value={cat}>
                      {CATEGORY_META[cat].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                onClick={() => fileInputRef.current?.click()}
                disabled={isUploading}
                variant="outline"
                size="sm"
              >
                {isUploading ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Uploading...
                  </>
                ) : (
                  <>
                    <Upload className="mr-2 h-4 w-4" />
                    Upload File
                  </>
                )}
              </Button>
              <span className="text-xs text-muted-foreground">
                .md, .txt, .pdf — max 50MB
              </span>
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
                          <Badge variant="secondary" className={`text-xs px-1.5 py-0 ${meta.color}`}>
                            {meta.label}
                          </Badge>
                          {entry.description && (
                            <span className="truncate max-w-[200px]">{entry.description}</span>
                          )}
                          {entry.updated_at && (
                            <span>{formatDate(entry.updated_at)}</span>
                          )}
                          {entry.last_edited_by && (
                            <span className="capitalize">by {entry.last_edited_by}</span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {canWrite && (
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleDelete(entry.id, entry.title)}
                          disabled={deletingId === entry.id}
                        >
                          {deletingId === entry.id ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
                          )}
                        </Button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <DiscoverySettings />
    </div>
  );
}

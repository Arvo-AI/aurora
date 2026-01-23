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
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/hooks/use-toast";
import { useUserId } from "@/hooks/use-user-id";
import {
  Loader2,
  Upload,
  Trash2,
  FileText,
  BookOpen,
  Brain,
  Check,
  AlertCircle,
  RefreshCw,
} from "lucide-react";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL;
const MEMORY_MAX_LENGTH = 5000;

interface Document {
  id: string;
  filename: string;
  original_filename: string;
  file_type: string;
  file_size_bytes: number;
  status: "uploading" | "processing" | "ready" | "failed";
  error_message: string | null;
  chunk_count: number;
  created_at: string;
  updated_at: string;
}

interface Usage {
  document_count: number;
  document_limit: number;
  storage_used_mb: number;
  storage_limit_mb: number;
}

export function KnowledgeBaseSettings() {
  const { userId, isLoading: userLoading } = useUserId();
  const { toast } = useToast();

  // Memory state
  const [memoryContent, setMemoryContent] = useState("");
  const [originalMemory, setOriginalMemory] = useState("");
  const [isLoadingMemory, setIsLoadingMemory] = useState(true);
  const [isSavingMemory, setIsSavingMemory] = useState(false);

  // Documents state
  const [documents, setDocuments] = useState<Document[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [isLoadingDocs, setIsLoadingDocs] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Polling for processing documents
  const [pollingIds, setPollingIds] = useState<Set<string>>(new Set());

  const hasMemoryChanges = memoryContent !== originalMemory;

  // Fetch memory
  const fetchMemory = useCallback(async () => {
    if (!userId) {
      setIsLoadingMemory(false);
      return;
    }

    try {
      const res = await fetch(`${BACKEND_URL}/api/knowledge-base/memory`, {
        headers: { "X-User-ID": userId },
      });

      if (res.ok) {
        const data = await res.json();
        setMemoryContent(data.content || "");
        setOriginalMemory(data.content || "");
      }
    } catch (error) {
      console.error("Failed to fetch memory:", error);
    } finally {
      setIsLoadingMemory(false);
    }
  }, [userId]);

  // Fetch documents
  const fetchDocuments = useCallback(async () => {
    if (!userId) {
      setIsLoadingDocs(false);
      setPollingIds(new Set());
      return;
    }

    try {
      const res = await fetch(`${BACKEND_URL}/api/knowledge-base/documents`, {
        headers: { "X-User-ID": userId },
      });

      if (res.ok) {
        const data = await res.json();
        setDocuments(data.documents || []);
        setUsage(data.usage || null);

        // Track processing documents for polling
        const processingIds = new Set<string>(
          data.documents
            .filter((d: Document) => d.status === "processing" || d.status === "uploading")
            .map((d: Document) => d.id)
        );
        setPollingIds(processingIds);
      } else {
        // Stop polling on error response
        setPollingIds(new Set());
      }
    } catch (error) {
      console.error("Failed to fetch documents:", error);
      setPollingIds(new Set());
    } finally {
      setIsLoadingDocs(false);
    }
  }, [userId]);

  // Initial fetch
  useEffect(() => {
    if (userId && !userLoading) {
      fetchMemory();
      fetchDocuments();
    }
  }, [userId, userLoading, fetchMemory, fetchDocuments]);

  // Polling for processing documents
  useEffect(() => {
    if (pollingIds.size === 0) return;

    const interval = setInterval(() => {
      fetchDocuments();
    }, 3000);

    return () => clearInterval(interval);
  }, [pollingIds, fetchDocuments]);

  // Save memory
  const handleSaveMemory = async () => {
    if (!userId) return;

    setIsSavingMemory(true);
    try {
      const res = await fetch(`${BACKEND_URL}/api/knowledge-base/memory`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-User-ID": userId,
        },
        body: JSON.stringify({ content: memoryContent }),
      });

      if (res.ok) {
        setOriginalMemory(memoryContent);
        toast({
          title: "Memory saved",
          description: "Your context will be used in all future conversations.",
        });
      } else {
        const data = await res.json();
        throw new Error(data.error || "Failed to save memory");
      }
    } catch (error) {
      toast({
        title: "Failed to save memory",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setIsSavingMemory(false);
    }
  };

  // Upload document
  const handleUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file || !userId) return;

    // Validate file type
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

    // Validate file size (50MB)
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

    try {
      const res = await fetch(`${BACKEND_URL}/api/knowledge-base/upload`, {
        method: "POST",
        headers: { "X-User-ID": userId },
        body: formData,
      });

      if (res.ok) {
        toast({
          title: "Document uploaded",
          description: "Processing in background. This may take a few moments.",
        });
        try {
          await fetchDocuments();
        } catch (error) {
          console.error("Failed to refresh documents list:", error);
          toast({
            title: "List refresh failed",
            description: "Your document was uploaded but the list didn't refresh. Try reloading the page.",
          });
        }
      } else {
        const data = await res.json();
        throw new Error(data.error || "Failed to upload document");
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

  // Delete document
  const handleDelete = async (docId: string, filename: string) => {
    if (!userId) return;

    setDeletingId(docId);
    try {
      const res = await fetch(
        `${BACKEND_URL}/api/knowledge-base/documents/${docId}`,
        {
          method: "DELETE",
          headers: { "X-User-ID": userId },
        }
      );

      if (res.ok) {
        toast({
          title: "Document deleted",
          description: `"${filename}" has been removed from your knowledge base.`,
        });
        try {
          await fetchDocuments();
        } catch (error) {
          console.error("Failed to refresh documents list:", error);
          toast({
            title: "List refresh failed",
            description: "The document was deleted but the list didn't refresh. Try reloading the page.",
          });
        }
      } else {
        const data = await res.json();
        throw new Error(data.error || "Failed to delete document");
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

  function getStatusBadge(status: Document["status"]) {
    const statusConfig = {
      uploading: {
        variant: "secondary" as const,
        className: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
        icon: <Loader2 className="mr-1 h-3 w-3 animate-spin" />,
        label: "Uploading",
      },
      processing: {
        variant: "secondary" as const,
        className: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
        icon: <RefreshCw className="mr-1 h-3 w-3 animate-spin" />,
        label: "Processing",
      },
      ready: {
        variant: "secondary" as const,
        className: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
        icon: <Check className="mr-1 h-3 w-3" />,
        label: "Ready",
      },
      failed: {
        variant: "destructive" as const,
        className: "",
        icon: <AlertCircle className="mr-1 h-3 w-3" />,
        label: "Failed",
      },
    };

    const config = statusConfig[status];
    if (!config) return null;

    return (
      <Badge variant={config.variant} className={config.className || undefined}>
        {config.icon}
        {config.label}
      </Badge>
    );
  }

  function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  if (userLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Knowledge Base</h1>
        <p className="text-muted-foreground">
          Manage your team&apos;s documentation and context for Aurora to reference.
        </p>
      </div>

      {/* Memory Section */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Brain className="h-5 w-5 text-primary" />
            <CardTitle>Memory</CardTitle>
          </div>
          <CardDescription>
            Context that Aurora always remembers. Add key facts, patterns, and
            conventions your team uses. This is included in every conversation.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {isLoadingMemory ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : (
            <>
              <Textarea
                value={memoryContent}
                onChange={(e) => setMemoryContent(e.target.value)}
                placeholder="## Query Patterns&#10;- Error logs: index=prod level=ERROR&#10;&#10;## Service Map&#10;- prod-api: Main gateway&#10;- prod-db: Cloud Spanner&#10;&#10;## Escalation&#10;- Spanner issues: @db-oncall"
                className="min-h-[200px] font-mono text-sm"
                maxLength={MEMORY_MAX_LENGTH}
              />
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">
                  {memoryContent.length} / {MEMORY_MAX_LENGTH} characters
                </span>
                <Button
                  onClick={handleSaveMemory}
                  disabled={isSavingMemory || !hasMemoryChanges}
                >
                  {isSavingMemory ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Saving...
                    </>
                  ) : (
                    "Save"
                  )}
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {/* Documents Section */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <BookOpen className="h-5 w-5 text-primary" />
            <CardTitle>Documents</CardTitle>
          </div>
          <CardDescription>
            Upload runbooks, architecture docs, and postmortems for Aurora to
            search during investigations.
          </CardDescription>
          {usage && (
            <div className="mt-2 text-xs text-muted-foreground">
              {usage.document_count}/{usage.document_limit} documents · {usage.storage_used_mb}/{usage.storage_limit_mb} MB used
            </div>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Upload Button */}
          <div>
            <input
              ref={fileInputRef}
              type="file"
              accept=".md,.txt,.pdf"
              onChange={handleUpload}
              className="hidden"
              id="document-upload"
            />
            <Button
              onClick={() => fileInputRef.current?.click()}
              disabled={isUploading}
              variant="outline"
            >
              {isUploading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Uploading...
                </>
              ) : (
                <>
                  <Upload className="mr-2 h-4 w-4" />
                  Upload Document
                </>
              )}
            </Button>
            <p className="mt-2 text-sm text-muted-foreground">
              Supported formats: Markdown (.md), Plain Text (.txt), PDF (.pdf).
              Max size: 50MB.
            </p>
          </div>

          {/* Documents List */}
          {isLoadingDocs ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : documents.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center">
              <FileText className="mx-auto h-12 w-12 text-muted-foreground/50" />
              <p className="mt-2 text-sm text-muted-foreground">
                No documents uploaded yet. Upload your first document to get
                started.
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {documents.map((doc) => (
                <div
                  key={doc.id}
                  className="flex items-center justify-between rounded-lg border p-4"
                >
                  <div className="flex items-center gap-3">
                    <FileText className="h-8 w-8 text-muted-foreground" />
                    <div>
                      <p className="font-medium">{doc.original_filename}</p>
                      <div className="flex items-center gap-2 text-sm text-muted-foreground">
                        <span>{formatFileSize(doc.file_size_bytes)}</span>
                        {doc.status === "ready" && doc.chunk_count > 0 && (
                          <>
                            <span>-</span>
                            <span>{doc.chunk_count} chunks</span>
                          </>
                        )}
                        {doc.status === "failed" && doc.error_message && (
                          <>
                            <span>-</span>
                            <span className="text-destructive">
                              {doc.error_message}
                            </span>
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    {getStatusBadge(doc.status)}
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => handleDelete(doc.id, doc.original_filename)}
                      disabled={deletingId === doc.id}
                    >
                      {deletingId === doc.id ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
                      )}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

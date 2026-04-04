"use client";

import React, { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite as checkCanWrite } from "@/lib/roles";
import {
  Plus,
  Pencil,
  Trash2,
  Lock,
  Download,
  Loader2,
  Search,
  ExternalLink,
  Check,
} from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { fetchR } from "@/lib/query";

interface Skill {
  id: string;
  name: string;
  description: string;
  body?: string;
  tags: string[];
  providers: string[];
  mode_restriction: string | null;
  prompt_behavior: string;
  scope: string;
  user_id: string | null;
  org_id: string | null;
  is_active: boolean;
  version: string;
  references_data?: Record<string, string>;
  created_at: string | null;
  updated_at: string | null;
}

interface SkillFormData {
  name: string;
  description: string;
  body: string;
  scope: string;
  prompt_behavior: string;
  tags: string;
  providers: string;
  mode_restriction: string;
  is_active: boolean;
  references_data: string;
}

function formToPayload(form: SkillFormData) {
  const parseCsv = (s: string) =>
    s.split(",").map((t) => t.trim()).filter(Boolean);
  return {
    name: form.name,
    description: form.description,
    body: form.body,
    scope: form.scope,
    prompt_behavior: form.prompt_behavior,
    tags: parseCsv(form.tags),
    providers: parseCsv(form.providers),
    mode_restriction: form.mode_restriction || null,
    is_active: form.is_active,
  };
}

interface DiscoveredSkill {
  name: string;
  description: string;
  path: string;
}

const EMPTY_FORM: SkillFormData = {
  name: "",
  description: "",
  body: "",
  scope: "org",
  prompt_behavior: "supplement",
  tags: "",
  providers: "",
  mode_restriction: "",
  is_active: true,
  references_data: "{}",
};

function scopeColor(scope: string) {
  switch (scope) {
    case "global":
      return "bg-blue-500/10 text-blue-400 border-blue-500/20";
    case "org":
      return "bg-green-500/10 text-green-400 border-green-500/20";
    case "user":
      return "bg-purple-500/10 text-purple-400 border-purple-500/20";
    default:
      return "";
  }
}

function behaviorColor(behavior: string) {
  switch (behavior) {
    case "supplement":
      return "bg-zinc-500/10 text-zinc-400 border-zinc-500/20";
    case "override":
      return "bg-amber-500/10 text-amber-400 border-amber-500/20";
    case "exclusive":
      return "bg-red-500/10 text-red-400 border-red-500/20";
    default:
      return "";
  }
}

export function SkillsSettings() {
  const { user } = useUser();
  const canWrite = checkCanWrite(user?.role);
  const { toast } = useToast();

  const [skills, setSkills] = useState<Skill[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState<Skill | null>(null);
  const [showImportDialog, setShowImportDialog] = useState(false);
  const [formData, setFormData] = useState<SkillFormData>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);

  // Import state
  const [importUrl, setImportUrl] = useState("");
  const [discovering, setDiscovering] = useState(false);
  const [discoveredSkills, setDiscoveredSkills] = useState<DiscoveredSkill[]>(
    []
  );
  const [selectedImports, setSelectedImports] = useState<Set<string>>(
    new Set()
  );
  const [installing, setInstalling] = useState(false);
  const [importRepoInfo, setImportRepoInfo] = useState<{
    owner: string;
    repo: string;
  } | null>(null);

  const showError = (fallback: string, error?: unknown) => {
    toast({
      title: "Error",
      description: error instanceof Error ? error.message : fallback,
      variant: "destructive",
    });
  };

  const closeFormDialog = () => {
    setShowCreateDialog(false);
    setEditingSkill(null);
    setFormData(EMPTY_FORM);
  };

  const closeImportDialog = () => {
    setShowImportDialog(false);
    setImportUrl("");
    setDiscoveredSkills([]);
    setSelectedImports(new Set());
    setImportRepoInfo(null);
  };

  const fetchSkills = async () => {
    try {
      const res = await fetch("/api/skills", { credentials: "include" });
      if (!res.ok) throw new Error("Failed to fetch");
      const data = await res.json();
      setSkills(data.skills || []);
    } catch (error) {
      showError("Failed to load skills", error);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchSkills();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleCreate = async () => {
    setSaving(true);
    try {
      const payload = formToPayload(formData);
      const res = await fetchR("/api/skills", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to create");
      toast({ title: "Skill created", description: `"${formData.name}" added` });
      closeFormDialog();
      fetchSkills();
    } catch (error) {
      showError("Failed to create skill", error);
    } finally {
      setSaving(false);
    }
  };

  const handleUpdate = async () => {
    if (!editingSkill) return;
    setSaving(true);
    try {
      const payload = formToPayload(formData);
      const res = await fetchR(`/api/skills/${editingSkill.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to update");
      toast({ title: "Skill updated" });
      closeFormDialog();
      fetchSkills();
    } catch (error) {
      showError("Failed to update skill", error);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!showDeleteDialog) return;
    try {
      const res = await fetchR(`/api/skills/${showDeleteDialog.id}`, {
        method: "DELETE",
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to delete");
      toast({ title: "Skill deleted" });
      setShowDeleteDialog(null);
      fetchSkills();
    } catch (error) {
      showError("Failed to delete skill", error);
    }
  };

  const handleToggleActive = async (skill: Skill) => {
    try {
      const res = await fetchR(`/api/skills/${skill.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ is_active: !skill.is_active }),
      });
      if (!res.ok) throw new Error("Failed to toggle");
      fetchSkills();
    } catch (error) {
      showError("Failed to toggle skill", error);
    }
  };

  const handleDiscover = async () => {
    setDiscovering(true);
    setDiscoveredSkills([]);
    setSelectedImports(new Set());
    setImportRepoInfo(null);
    try {
      const res = await fetchR("/api/skills/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ action: "discover", url: importUrl }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to discover");
      setDiscoveredSkills(data.skills || []);
      setImportRepoInfo({ owner: data.owner, repo: data.repo });
      if ((data.skills || []).length === 0) {
        toast({
          title: "No skills found",
          description: "No SKILL.md files found in this repository",
        });
      }
    } catch (error) {
      showError("Failed to discover skills", error);
    } finally {
      setDiscovering(false);
    }
  };

  const handleInstall = async () => {
    if (selectedImports.size === 0) return;
    setInstalling(true);
    try {
      const res = await fetchR("/api/skills/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          action: "install",
          url: importUrl,
          skill_paths: Array.from(selectedImports),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to install");
      const count = (data.installed || []).filter(
        (i: { error?: string }) => !i.error
      ).length;
      toast({
        title: "Import complete",
        description: `${count} skill(s) imported successfully`,
      });
      closeImportDialog();
      fetchSkills();
    } catch (error) {
      showError("Failed to install skills", error);
    } finally {
      setInstalling(false);
    }
  };

  const openEdit = async (skill: Skill) => {
    // Fetch full skill detail (with body)
    try {
      const res = await fetch(`/api/skills/${skill.id}`, {
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) throw new Error("Failed to fetch");
      setEditingSkill(data);
      setFormData({
        name: data.name || "",
        description: data.description || "",
        body: data.body || "",
        scope: data.scope || "org",
        prompt_behavior: data.prompt_behavior || "supplement",
        tags: (data.tags || []).join(", "),
        providers: (data.providers || []).join(", "),
        mode_restriction: data.mode_restriction || "",
        is_active: data.is_active ?? true,
        references_data: JSON.stringify(data.references_data || {}, null, 2),
      });
    } catch (error) {
      showError("Failed to load skill details", error);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Skills</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Manage procedural skills the agent can load on-demand during
            investigations.
          </p>
        </div>
        {canWrite && (
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowImportDialog(true)}
            >
              <Download className="h-4 w-4 mr-2" />
              Import from URL
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setFormData(EMPTY_FORM);
                setShowCreateDialog(true);
              }}
            >
              <Plus className="h-4 w-4 mr-2" />
              New Skill
            </Button>
          </div>
        )}
      </div>

      {skills.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <p className="text-muted-foreground mb-4">
              No skills configured yet. Create a skill or import from a GitHub
              repository.
            </p>
            {canWrite && (
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  onClick={() => setShowImportDialog(true)}
                >
                  <Download className="h-4 w-4 mr-2" />
                  Import from URL
                </Button>
                <Button
                  onClick={() => {
                    setFormData(EMPTY_FORM);
                    setShowCreateDialog(true);
                  }}
                >
                  <Plus className="h-4 w-4 mr-2" />
                  New Skill
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {skills.map((skill) => (
            <Card key={skill.id} className={!skill.is_active ? "opacity-50" : ""}>
              <CardContent className="py-4 px-5">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      {skill.scope === "global" && (
                        <Lock className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                      )}
                      <span className="font-medium truncate">
                        {skill.name}
                      </span>
                      <Badge variant="outline" className={scopeColor(skill.scope)}>
                        {skill.scope === "global"
                          ? "Global"
                          : skill.scope === "org"
                            ? "Org"
                            : "Private"}
                      </Badge>
                      <Badge
                        variant="outline"
                        className={behaviorColor(skill.prompt_behavior)}
                      >
                        {skill.prompt_behavior}
                      </Badge>
                    </div>
                    <p className="text-sm text-muted-foreground line-clamp-2">
                      {skill.description}
                    </p>
                    <div className="flex items-center gap-2 mt-2 flex-wrap">
                      {skill.tags.map((tag) => (
                        <Badge key={tag} variant="secondary" className="text-xs">
                          {tag}
                        </Badge>
                      ))}
                      {skill.providers.length > 0 && (
                        <span className="text-xs text-muted-foreground">
                          Providers: {skill.providers.join(", ")}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    {canWrite && skill.scope !== "global" && (
                      <>
                        <Switch
                          checked={skill.is_active}
                          onCheckedChange={() => handleToggleActive(skill)}
                        />
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => openEdit(skill)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => setShowDeleteDialog(skill)}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Create / Edit Dialog */}
      <Dialog
        open={showCreateDialog || !!editingSkill}
        onOpenChange={(open) => {
          if (!open) closeFormDialog();
        }}
      >
        <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
          <DialogTitle>
            {editingSkill ? "Edit Skill" : "Create Skill"}
          </DialogTitle>
          <DialogDescription>
            {editingSkill
              ? "Update the skill configuration and content."
              : "Define a new procedural skill the agent can load on-demand."}
          </DialogDescription>
          <div className="space-y-4 mt-2">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Name</Label>
                <Input
                  placeholder="e.g. datadog-rca"
                  value={formData.name}
                  onChange={(e) =>
                    setFormData((f) => ({ ...f, name: e.target.value }))
                  }
                />
              </div>
              <div className="space-y-2">
                <Label>Scope</Label>
                <Select
                  value={formData.scope}
                  onValueChange={(v) =>
                    setFormData((f) => ({ ...f, scope: v }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="org">Organization</SelectItem>
                    <SelectItem value="user">Private (just me)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="space-y-2">
              <Label>Description</Label>
              <Input
                placeholder="Short description shown in the agent's skill catalog"
                value={formData.description}
                onChange={(e) =>
                  setFormData((f) => ({ ...f, description: e.target.value }))
                }
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Prompt Behavior</Label>
                <Select
                  value={formData.prompt_behavior}
                  onValueChange={(v) =>
                    setFormData((f) => ({ ...f, prompt_behavior: v }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="supplement">
                      Supplement (adds to prompt)
                    </SelectItem>
                    <SelectItem value="override">
                      Override (skill takes priority)
                    </SelectItem>
                    <SelectItem value="exclusive">
                      Exclusive (replaces prompt segment)
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Mode Restriction</Label>
                <Select
                  value={formData.mode_restriction || "any"}
                  onValueChange={(v) =>
                    setFormData((f) => ({
                      ...f,
                      mode_restriction: v === "any" ? "" : v,
                    }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="any">Any mode</SelectItem>
                    <SelectItem value="agent">Agent only</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Tags (comma-separated)</Label>
                <Input
                  placeholder="rca, observability, aws"
                  value={formData.tags}
                  onChange={(e) =>
                    setFormData((f) => ({ ...f, tags: e.target.value }))
                  }
                />
              </div>
              <div className="space-y-2">
                <Label>Providers (comma-separated)</Label>
                <Input
                  placeholder="aws, gcp (empty = universal)"
                  value={formData.providers}
                  onChange={(e) =>
                    setFormData((f) => ({ ...f, providers: e.target.value }))
                  }
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label>Skill Body (Markdown)</Label>
              <Textarea
                placeholder="# Skill Instructions&#10;&#10;Step 1: ..."
                className="min-h-[200px] font-mono text-sm"
                value={formData.body}
                onChange={(e) =>
                  setFormData((f) => ({ ...f, body: e.target.value }))
                }
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeFormDialog}>
              Cancel
            </Button>
            <Button
              onClick={editingSkill ? handleUpdate : handleCreate}
              disabled={saving || !formData.name || !formData.description || !formData.body}
            >
              {saving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {editingSkill ? "Save Changes" : "Create Skill"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog
        open={!!showDeleteDialog}
        onOpenChange={(open) => {
          if (!open) setShowDeleteDialog(null);
        }}
      >
        <DialogContent>
          <DialogTitle>Delete Skill</DialogTitle>
          <DialogDescription>
            Are you sure you want to delete &quot;{showDeleteDialog?.name}&quot;?
            This action cannot be undone.
          </DialogDescription>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowDeleteDialog(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Import from URL Dialog */}
      <Dialog
        open={showImportDialog}
        onOpenChange={(open) => {
          if (!open) closeImportDialog();
        }}
      >
        <DialogContent className="max-w-xl max-h-[85vh] overflow-y-auto">
          <DialogTitle>Import Skills from GitHub</DialogTitle>
          <DialogDescription>
            Enter a GitHub repository URL or owner/repo shorthand to discover
            and import skills.
          </DialogDescription>
          <div className="space-y-4 mt-2">
            <div className="flex gap-2">
              <Input
                placeholder="e.g. anthropics/skills or https://github.com/owner/repo"
                value={importUrl}
                onChange={(e) => setImportUrl(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && importUrl.trim()) handleDiscover();
                }}
              />
              <Button
                onClick={handleDiscover}
                disabled={discovering || !importUrl.trim()}
              >
                {discovering ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Search className="h-4 w-4" />
                )}
              </Button>
            </div>

            {importRepoInfo && (
              <div className="flex items-center gap-1 text-sm text-muted-foreground">
                <ExternalLink className="h-3.5 w-3.5" />
                {importRepoInfo.owner}/{importRepoInfo.repo}
              </div>
            )}

            {discoveredSkills.length > 0 && (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>
                    Found {discoveredSkills.length} skill(s)
                  </Label>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      if (selectedImports.size === discoveredSkills.length) {
                        setSelectedImports(new Set());
                      } else {
                        setSelectedImports(
                          new Set(discoveredSkills.map((s) => s.path))
                        );
                      }
                    }}
                  >
                    {selectedImports.size === discoveredSkills.length
                      ? "Deselect All"
                      : "Select All"}
                  </Button>
                </div>
                <div className="space-y-2 max-h-[300px] overflow-y-auto">
                  {discoveredSkills.map((skill) => (
                    <div
                      key={skill.path}
                      className="flex items-start gap-3 p-3 rounded-lg border border-border"
                    >
                      <Checkbox
                        checked={selectedImports.has(skill.path)}
                        onCheckedChange={(checked) => {
                          setSelectedImports((prev) => {
                            const next = new Set(prev);
                            if (checked) {
                              next.add(skill.path);
                            } else {
                              next.delete(skill.path);
                            }
                            return next;
                          });
                        }}
                      />
                      <div className="flex-1 min-w-0">
                        <div className="font-medium text-sm">{skill.name}</div>
                        {skill.description && (
                          <div className="text-xs text-muted-foreground mt-0.5">
                            {skill.description}
                          </div>
                        )}
                        <div className="text-xs text-muted-foreground/60 mt-0.5 truncate">
                          {skill.path}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeImportDialog}>
              Cancel
            </Button>
            {discoveredSkills.length > 0 && (
              <Button
                onClick={handleInstall}
                disabled={installing || selectedImports.size === 0}
              >
                {installing && (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                )}
                <Check className="h-4 w-4 mr-2" />
                Import {selectedImports.size} Selected
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

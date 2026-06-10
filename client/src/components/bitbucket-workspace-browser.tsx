'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, Pencil, RotateCw, X, RefreshCw } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { BitbucketIntegrationService } from '@/services/bitbucket-integration-service';
import type { Workspace, Repo } from '@/services/bitbucket-integration-service';

interface ConnectedRepo {
  slug: string;
  full_name: string;
  workspace: string | null;
  default_branch: string | null;
  metadata_summary: string | null;
  metadata_status: string | null;
}

function parseConnectedRepos(repositories: NonNullable<import('@/services/bitbucket-integration-service').WorkspaceSelectionResponse['repositories']>): ConnectedRepo[] {
  return repositories
    .filter((r): r is Exclude<typeof r, string> => typeof r !== 'string')
    .map(r => ({
      slug: r.slug,
      full_name: r.full_name || '',
      workspace: r.workspace || null,
      default_branch: r.default_branch || null,
      metadata_summary: r.metadata_summary || null,
      metadata_status: r.metadata_status || null,
    }));
}

export default function BitbucketWorkspaceBrowser() {
  const { toast } = useToast();

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string>('');
  const [isLoadingWorkspaces, setIsLoadingWorkspaces] = useState(false);

  const [repos, setRepos] = useState<Repo[]>([]);
  const [checkedRepos, setCheckedRepos] = useState<Set<string>>(new Set());
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const isRestoringSelectionRef = useRef(false);
  // Map of workspace → set of saved slugs (supports multi-workspace)
  const [savedReposByWorkspace, setSavedReposByWorkspace] = useState<Map<string, Set<string>>>(new Map());
  const savedReposByWorkspaceRef = useRef<Map<string, Set<string>>>(new Map());

  // Connected repos with metadata status (for the "Connected Repositories" section)
  const [savedRepos, setSavedRepos] = useState<ConnectedRepo[]>([]);
  const [editingMetadata, setEditingMetadata] = useState<Record<string, string>>({});
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const startMetadataPolling = useCallback((repos: ConnectedRepo[]) => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    const hasPending = repos.some(r => r.metadata_status === 'pending' || r.metadata_status === 'generating');
    if (!hasPending) return;
    pollingRef.current = setInterval(async () => {
      try {
        const data = await BitbucketIntegrationService.loadWorkspaceSelection();
        if (!data?.repositories) return;
        const updated = parseConnectedRepos(data.repositories);
        setSavedRepos(updated);
        const stillPending = updated.some(r => r.metadata_status === 'pending' || r.metadata_status === 'generating');
        if (!stillPending && pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
      } catch (err) {
        console.warn('[BitbucketWorkspaceBrowser] Metadata polling failed:', err);
      }
    }, 3000);
  }, []);

  useEffect(() => {
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, []);

  useEffect(() => {
    savedReposByWorkspaceRef.current = savedReposByWorkspace;
  }, [savedReposByWorkspace]);

  useEffect(() => {
    fetchWorkspaces();
    loadStoredSelection();
  }, []);

  // Re-fetch repos when the parent triggers a refresh (e.g. via the Refresh button)
  useEffect(() => {
    const handler = () => {
      fetchWorkspaces();
      if (selectedWorkspace) fetchRepos(selectedWorkspace);
    };
    window.addEventListener('bitbucketRefresh', handler);
    return () => window.removeEventListener('bitbucketRefresh', handler);
  }, [selectedWorkspace]);

  useEffect(() => {
    if (isRestoringSelectionRef.current) return;
    if (selectedWorkspace) {
      fetchRepos(selectedWorkspace);
    }
  }, [selectedWorkspace]);

  const fetchWorkspaces = async () => {
    setIsLoadingWorkspaces(true);
    try {
      const data = await BitbucketIntegrationService.getWorkspaces();
      const workspaceList = Array.isArray(data) ? data : data?.workspaces || [];
      setWorkspaces(workspaceList);
    } catch (error) {
      console.error('Error fetching workspaces:', error);
      setWorkspaces([]);
    } finally {
      setIsLoadingWorkspaces(false);
    }
  };

  const fetchRepos = async (workspace: string) => {
    setIsLoadingRepos(true);
    try {
      const data = await BitbucketIntegrationService.getRepos(workspace);
      const repoList = Array.isArray(data) ? data : data?.repositories || [];
      setRepos(repoList);
      // Use ref to always read the latest saved state (avoids stale closure)
      const saved = savedReposByWorkspaceRef.current.get(workspace);
      setCheckedRepos(saved ? new Set(saved) : new Set());
    } catch (error) {
      console.error('Error fetching repos:', error);
      setRepos([]);
      setCheckedRepos(new Set());
    } finally {
      setIsLoadingRepos(false);
    }
  };

  const toggleRepo = (slug: string) => {
    setCheckedRepos(prev => {
      const next = new Set(prev);
      if (next.has(slug)) {
        next.delete(slug);
      } else {
        next.add(slug);
      }
      return next;
    });
  };

  const loadStoredSelection = async () => {
    try {
      const data = await BitbucketIntegrationService.loadWorkspaceSelection();
      if (!data?.repositories || !Array.isArray(data.repositories) || data.repositories.length === 0) return;

      // Build the saved map from all returned repos (each has a workspace field)
      const byWorkspace = new Map<string, Set<string>>();
      const connected = parseConnectedRepos(data.repositories);
      for (const repo of connected) {
        const ws = repo.workspace || data.workspace || '';
        const slug = repo.slug;
        if (!ws || !slug) continue;
        if (!byWorkspace.has(ws)) byWorkspace.set(ws, new Set());
        byWorkspace.get(ws)!.add(slug);
      }
      setSavedReposByWorkspace(byWorkspace);
      savedReposByWorkspaceRef.current = byWorkspace;
      setSavedRepos(connected);
      startMetadataPolling(connected);

      // Set the active workspace to the first one with saved repos
      const firstWorkspace = data.workspace || byWorkspace.keys().next().value;
      if (firstWorkspace) {
        isRestoringSelectionRef.current = true;
        setSelectedWorkspace(firstWorkspace);

        const repoData = await BitbucketIntegrationService.getRepos(firstWorkspace);
        const repoList = Array.isArray(repoData) ? repoData : repoData?.repositories || [];
        setRepos(repoList);

        const saved = byWorkspace.get(firstWorkspace);
        setCheckedRepos(saved ? new Set(saved) : new Set());
        isRestoringSelectionRef.current = false;
      }
    } catch (error) {
      console.error('Error loading stored selection:', error);
      isRestoringSelectionRef.current = false;
    }
  };

  const handleSave = async () => {
    if (!selectedWorkspace || checkedRepos.size === 0) {
      toast({ title: "Error", description: "Select at least one repository", variant: "destructive" });
      return;
    }
    setIsSaving(true);
    try {
      const selectedRepoObjects = repos.filter(r => checkedRepos.has(r.slug));
      await BitbucketIntegrationService.saveWorkspaceSelection({
        workspace: selectedWorkspace,
        repositories: selectedRepoObjects,
      });
      setSavedReposByWorkspace(prev => {
        const next = new Map(prev);
        next.set(selectedWorkspace, new Set(checkedRepos));
        savedReposByWorkspaceRef.current = next;
        return next;
      });
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Saved", description: `${checkedRepos.size} repo${checkedRepos.size > 1 ? 's' : ''} connected` });

      // Refresh connected repos to show metadata generation status
      const data = await BitbucketIntegrationService.loadWorkspaceSelection();
      if (data?.repositories) {
        const connected = parseConnectedRepos(data.repositories);
        setSavedRepos(connected);
        startMetadataPolling(connected);
      }
    } catch (error: unknown) {
      const err = error as Error;
      console.error('Error saving selection:', err);
      toast({ title: "Error", description: err.message || "Failed to save selection", variant: "destructive" });
    } finally {
      setIsSaving(false);
    }
  };

  const handleClear = async () => {
    try {
      await BitbucketIntegrationService.clearWorkspaceSelection();
      setSelectedWorkspace('');
      setCheckedRepos(new Set());
      setRepos([]);
      setSavedReposByWorkspace(new Map());
      savedReposByWorkspaceRef.current = new Map();
      setSavedRepos([]);
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Cleared", description: "Bitbucket repos disconnected" });
    } catch (error: unknown) {
      const err = error as Error;
      console.error('Error clearing selection:', err);
      toast({ title: "Error", description: err.message || "Failed to clear", variant: "destructive" });
    }
  };

  const handleRegenerate = async (repoFullName: string) => {
    try {
      await BitbucketIntegrationService.generateRepoMetadata(repoFullName);
      const updated = savedRepos.map(r =>
        r.full_name === repoFullName ? { ...r, metadata_status: 'generating' } : r
      );
      setSavedRepos(updated);
      startMetadataPolling(updated);
    } catch {
      toast({ title: "Error", description: "Failed to regenerate description", variant: "destructive" });
    }
  };

  const handleSaveMetadata = async (repoFullName: string) => {
    const summary = editingMetadata[repoFullName];
    if (summary === undefined) return;
    try {
      await BitbucketIntegrationService.updateRepoMetadata(repoFullName, summary);
      setSavedRepos(prev => prev.map(r =>
        r.full_name === repoFullName ? { ...r, metadata_summary: summary, metadata_status: 'ready' } : r
      ));
      setEditingMetadata(prev => {
        const next = { ...prev };
        delete next[repoFullName];
        return next;
      });
    } catch {
      toast({ title: "Error", description: "Failed to save description", variant: "destructive" });
    }
  };

  const totalSavedRepos = Array.from(savedReposByWorkspace.values()).reduce((sum, set) => sum + set.size, 0);
  const currentWorkspaceSaved = savedReposByWorkspace.get(selectedWorkspace);
  const selectionChanged = selectedWorkspace && (
    checkedRepos.size !== (currentWorkspaceSaved?.size ?? 0) ||
    [...checkedRepos].some(s => !currentWorkspaceSaved?.has(s))
  );

  return (
    <div className="space-y-3">
      <div>
        <span className="text-sm font-medium mb-1.5 block">Workspace</span>
        {isLoadingWorkspaces ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin" />
            Loading workspaces...
          </div>
        ) : (
          <Select value={selectedWorkspace} onValueChange={setSelectedWorkspace}>
            <SelectTrigger>
              <SelectValue placeholder="Select a workspace..." />
            </SelectTrigger>
            <SelectContent>
              {workspaces.map((ws) => (
                <SelectItem key={ws.slug} value={ws.slug}>
                  <span className="flex items-center gap-2">
                    {ws.name || ws.slug}
                    {savedReposByWorkspace.has(ws.slug) && (
                      <Badge variant="secondary" className="text-xs ml-1">
                        {savedReposByWorkspace.get(ws.slug)!.size} saved
                      </Badge>
                    )}
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>

      {selectedWorkspace && (
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">Repositories</span>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 w-6 p-0"
                onClick={() => fetchRepos(selectedWorkspace)}
                disabled={isLoadingRepos}
                title="Refresh repository list"
              >
                <RefreshCw className={`h-3 w-3 ${isLoadingRepos ? 'animate-spin' : ''}`} />
              </Button>
            </div>
            {checkedRepos.size > 0 && (
              <Badge variant="outline" className="text-xs">{checkedRepos.size} selected</Badge>
            )}
          </div>
          {isLoadingRepos ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin" />
              Loading repositories...
            </div>
          ) : repos.length > 0 ? (
            <div className="space-y-1 max-h-48 overflow-y-auto border border-border rounded-lg p-2">
              {repos.map((repo) => (
                <label
                  key={repo.slug}
                  className="w-full flex items-center gap-3 p-2 rounded-md cursor-pointer hover:bg-muted/30 transition-colors"
                >
                  <Checkbox
                    checked={checkedRepos.has(repo.slug)}
                    onCheckedChange={() => toggleRepo(repo.slug)}
                  />
                  <div className="flex flex-col min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium truncate">{repo.name}</span>
                      <Badge variant={repo.is_private ? "secondary" : "outline"} className="text-xs">
                        {repo.is_private ? 'Private' : 'Public'}
                      </Badge>
                    </div>
                    {repo.mainbranch?.name && (
                      <span className="text-xs text-muted-foreground mt-0.5">
                        {repo.mainbranch.name}
                      </span>
                    )}
                  </div>
                  {currentWorkspaceSaved?.has(repo.slug) && (
                    <Check className="w-3.5 h-3.5 text-green-500 flex-shrink-0" />
                  )}
                </label>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No repositories found in this workspace.</p>
          )}
        </div>
      )}

      {selectedWorkspace && repos.length > 0 && (
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            onClick={handleSave}
            disabled={isSaving || checkedRepos.size === 0 || !selectionChanged}
          >
            {isSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" /> : null}
            Save
          </Button>
          {totalSavedRepos > 0 && (
            <Button size="sm" variant="outline" onClick={handleClear}>
              Clear All
            </Button>
          )}
        </div>
      )}

      {savedRepos.length > 0 && (
        <div className="space-y-2 pt-2 border-t border-border">
          <p className="text-sm font-medium text-muted-foreground">Connected Repositories</p>
          {savedRepos.map(repo => {
            const isEditing = editingMetadata[repo.full_name] !== undefined;
            const isReady = repo.metadata_status === 'ready';
            const isPending = repo.metadata_status === 'pending' || repo.metadata_status === 'generating';
            const isError = repo.metadata_status === 'error';
            return (
              <div key={repo.full_name} className="p-2 rounded-md border border-border space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-medium truncate">{repo.full_name}</span>
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    {isReady && (
                      <>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-6 w-6 p-0"
                          onClick={() => {
                            setEditingMetadata(prev => {
                              if (isEditing) {
                                const next = { ...prev };
                                delete next[repo.full_name];
                                return next;
                              }
                              return { ...prev, [repo.full_name]: repo.metadata_summary || '' };
                            });
                          }}
                          title={isEditing ? 'Cancel edit' : 'Edit description'}
                        >
                          {isEditing ? <X className="h-3 w-3" /> : <Pencil className="h-3 w-3" />}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-6 w-6 p-0"
                          onClick={() => handleRegenerate(repo.full_name)}
                          title="Regenerate description"
                        >
                          <RotateCw className="h-3 w-3" />
                        </Button>
                      </>
                    )}
                    {isError && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 px-2 text-xs"
                        onClick={() => handleRegenerate(repo.full_name)}
                      >
                        Retry
                      </Button>
                    )}
                  </div>
                </div>

                {isPending && (
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Generating description...
                  </div>
                )}
                {isError && (
                  <p className="text-xs text-red-500">Failed to generate description</p>
                )}
                {isReady && isEditing && (
                  <div className="space-y-1">
                    <Textarea
                      value={editingMetadata[repo.full_name]}
                      onChange={e => setEditingMetadata(prev => ({ ...prev, [repo.full_name]: e.target.value }))}
                      className="text-xs min-h-[60px]"
                      rows={2}
                    />
                    <Button size="sm" className="h-6 text-xs" onClick={() => handleSaveMetadata(repo.full_name)}>
                      Save
                    </Button>
                  </div>
                )}
                {isReady && !isEditing && repo.metadata_summary && (
                  <p className="text-xs text-muted-foreground line-clamp-2">{repo.metadata_summary}</p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

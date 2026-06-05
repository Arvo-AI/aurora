'use client';

import { useState, useEffect, useRef } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { BitbucketIntegrationService } from '@/services/bitbucket-integration-service';
import type { Workspace, Repo } from '@/services/bitbucket-integration-service';

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

  useEffect(() => {
    fetchWorkspaces();
    loadStoredSelection();
  }, []);

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
      // Restore checked state from saved selections for this workspace
      const saved = savedReposByWorkspace.get(workspace);
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
      for (const r of data.repositories) {
        if (typeof r === 'string') continue;
        const ws = (r as { workspace?: string }).workspace || data.workspace || '';
        const slug = (r as { slug: string }).slug;
        if (!ws || !slug) continue;
        if (!byWorkspace.has(ws)) byWorkspace.set(ws, new Set());
        byWorkspace.get(ws)!.add(slug);
      }
      setSavedReposByWorkspace(byWorkspace);

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
        return next;
      });
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Saved", description: `${checkedRepos.size} repo${checkedRepos.size > 1 ? 's' : ''} connected` });
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
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Cleared", description: "Bitbucket repos disconnected" });
    } catch (error: unknown) {
      const err = error as Error;
      console.error('Error clearing selection:', err);
      toast({ title: "Error", description: err.message || "Failed to clear", variant: "destructive" });
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
            <span className="text-sm font-medium">Repositories</span>
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
    </div>
  );
}

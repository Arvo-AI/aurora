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
  const [savedRepoSlugs, setSavedRepoSlugs] = useState<Set<string>>(new Set());

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
    setCheckedRepos(new Set());
    try {
      const data = await BitbucketIntegrationService.getRepos(workspace);
      const repoList = Array.isArray(data) ? data : data?.repositories || [];
      setRepos(repoList);
    } catch (error) {
      console.error('Error fetching repos:', error);
      setRepos([]);
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
      if (!data?.workspace) return;

      isRestoringSelectionRef.current = true;
      setSelectedWorkspace(data.workspace);

      const repoData = await BitbucketIntegrationService.getRepos(data.workspace);
      const repoList = Array.isArray(repoData) ? repoData : repoData?.repositories || [];
      setRepos(repoList);

      if (data.repositories && Array.isArray(data.repositories)) {
        const slugs = new Set(data.repositories.map((r: string | { slug: string }) =>
          typeof r === 'string' ? r : r.slug
        ));
        setCheckedRepos(slugs);
        setSavedRepoSlugs(slugs);
      } else if (data.repository) {
        const slug = typeof data.repository === 'string' ? data.repository : data.repository.slug;
        setCheckedRepos(new Set([slug]));
        setSavedRepoSlugs(new Set([slug]));
      }

      isRestoringSelectionRef.current = false;
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
      setSavedRepoSlugs(new Set(checkedRepos));
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Saved", description: `${checkedRepos.size} repo${checkedRepos.size > 1 ? 's' : ''} connected` });
    } catch (error: any) {
      console.error('Error saving selection:', error);
      toast({ title: "Error", description: error.message || "Failed to save selection", variant: "destructive" });
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
      setSavedRepoSlugs(new Set());
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Cleared", description: "Bitbucket repos disconnected" });
    } catch (error: any) {
      console.error('Error clearing selection:', error);
      toast({ title: "Error", description: error.message || "Failed to clear", variant: "destructive" });
    }
  };

  const selectionChanged = selectedWorkspace &&
    (checkedRepos.size !== savedRepoSlugs.size ||
     [...checkedRepos].some(s => !savedRepoSlugs.has(s)));

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
                  {ws.name || ws.slug}
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
                  {savedRepoSlugs.has(repo.slug) && (
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
          {savedRepoSlugs.size > 0 && (
            <Button size="sm" variant="outline" onClick={handleClear}>
              Clear All
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

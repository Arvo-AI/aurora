'use client';

import { useState, useEffect, useRef } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, GitBranch } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { BitbucketIntegrationService } from '@/services/bitbucket-integration-service';
import type { Workspace, Repo, Branch } from '@/services/bitbucket-integration-service';

interface BitbucketWorkspaceBrowserProps {
  userId: string;
}

export default function BitbucketWorkspaceBrowser({ userId }: BitbucketWorkspaceBrowserProps) {
  const { toast } = useToast();

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string>('');
  const [isLoadingWorkspaces, setIsLoadingWorkspaces] = useState(false);

  const [repos, setRepos] = useState<Repo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<Repo | null>(null);
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);

  const [branches, setBranches] = useState<Branch[]>([]);
  const [selectedBranch, setSelectedBranch] = useState<string>('');
  const [isLoadingBranches, setIsLoadingBranches] = useState(false);
  const isRestoringSelectionRef = useRef(false);
  const [savedSelection, setSavedSelection] = useState<{ workspace: string; repoSlug: string; branch: string } | null>(null);

  useEffect(() => {
    fetchWorkspaces();
    loadStoredSelection();
  }, [userId]);

  // Fetch repos when workspace changes (skip during selection restore)
  useEffect(() => {
    if (isRestoringSelectionRef.current) return;
    if (selectedWorkspace) {
      fetchRepos(selectedWorkspace);
    }
  }, [selectedWorkspace, userId]);

  const fetchWorkspaces = async () => {
    setIsLoadingWorkspaces(true);
    try {
      const data = await BitbucketIntegrationService.getWorkspaces(userId);
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
    setSelectedRepo(null);
    setBranches([]);
    setSelectedBranch('');
    try {
      const data = await BitbucketIntegrationService.getRepos(userId, workspace);
      const repoList = Array.isArray(data) ? data : data?.repositories || [];
      setRepos(repoList);
    } catch (error) {
      console.error('Error fetching repos:', error);
      setRepos([]);
    } finally {
      setIsLoadingRepos(false);
    }
  };

  const fetchBranches = async (workspace: string, repoSlug: string) => {
    setIsLoadingBranches(true);
    try {
      const data = await BitbucketIntegrationService.getBranches(userId, workspace, repoSlug);
      const branchList = Array.isArray(data) ? data : data?.branches || [];
      setBranches(branchList);
      if (branchList.length > 0) {
        const repo = repos.find(r => r.slug === repoSlug);
        const defaultBranchName = repo?.mainbranch?.name || 'main';
        const defaultBranch = branchList.find((b: Branch) => b.name === defaultBranchName);
        setSelectedBranch(defaultBranch ? defaultBranch.name : branchList[0].name);
      }
    } catch (error) {
      console.error('Error fetching branches:', error);
      setBranches([]);
    } finally {
      setIsLoadingBranches(false);
    }
  };

  const handleRepoSelect = (repo: Repo) => {
    setSelectedRepo(repo);
    setBranches([]);
    setSelectedBranch('');
    fetchBranches(selectedWorkspace, repo.slug);
  };

  const loadStoredSelection = async () => {
    try {
      const data = await BitbucketIntegrationService.loadWorkspaceSelection(userId);
      if (!data?.workspace) return;

      isRestoringSelectionRef.current = true;
      setSelectedWorkspace(data.workspace);

      const repoData = await BitbucketIntegrationService.getRepos(userId, data.workspace);
      const repoList = Array.isArray(repoData) ? repoData : repoData?.repositories || [];
      setRepos(repoList);

      if (data.repository) {
        const repoSlug = typeof data.repository === 'string' ? data.repository : data.repository.slug;
        const matchedRepo = repoList.find((r: Repo) => r.slug === repoSlug);
        if (matchedRepo) {
          setSelectedRepo(matchedRepo);

          const branchData = await BitbucketIntegrationService.getBranches(userId, data.workspace, matchedRepo.slug);
          const branchList = Array.isArray(branchData) ? branchData : branchData?.branches || [];
          setBranches(branchList);

          if (data.branch) {
            const branchName = typeof data.branch === 'string' ? data.branch : data.branch.name;
            setSelectedBranch(branchName);
            setSavedSelection({ workspace: data.workspace, repoSlug: matchedRepo.slug, branch: branchName });
          }
        }
      }

      isRestoringSelectionRef.current = false;
    } catch (error) {
      console.error('Error loading stored selection:', error);
      isRestoringSelectionRef.current = false;
    }
  };

  const handleSaveSelection = async () => {
    if (!selectedWorkspace || !selectedRepo || !selectedBranch) {
      toast({ title: "Error", description: "Please select a workspace, repository, and branch", variant: "destructive" });
      return;
    }
    try {
      await BitbucketIntegrationService.saveWorkspaceSelection(userId, {
        workspace: selectedWorkspace,
        repository: selectedRepo,
        branch: selectedBranch,
      });
      setSavedSelection({ workspace: selectedWorkspace, repoSlug: selectedRepo.slug, branch: selectedBranch });
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Selection Saved", description: `${selectedWorkspace} / ${selectedRepo.name} / ${selectedBranch}` });
    } catch (error: any) {
      console.error('Error saving selection:', error);
      toast({ title: "Error", description: error.message || "Failed to save selection", variant: "destructive" });
    }
  };

  const handleClearSelection = async () => {
    try {
      await BitbucketIntegrationService.clearWorkspaceSelection(userId);
      setSelectedWorkspace('');
      setSelectedRepo(null);
      setBranches([]);
      setSelectedBranch('');
      setRepos([]);
      setSavedSelection(null);
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Selection Cleared", description: "Bitbucket workspace selection cleared" });
    } catch (error: any) {
      console.error('Error clearing selection:', error);
      toast({ title: "Error", description: error.message || "Failed to clear selection", variant: "destructive" });
    }
  };

  const hasCompleteSelection = selectedWorkspace && selectedRepo && selectedBranch;
  const selectionMatchesSaved = hasCompleteSelection && savedSelection &&
    savedSelection.workspace === selectedWorkspace &&
    savedSelection.repoSlug === selectedRepo.slug &&
    savedSelection.branch === selectedBranch;

  return (
    <div className="space-y-3">
      <div>
        <label className="text-sm font-medium mb-1.5 block">Workspace</label>
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
            <label className="text-sm font-medium">Repositories</label>
            {repos.length > 0 && (
              <Badge variant="outline" className="text-xs">{repos.length} available</Badge>
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
                <div
                  key={repo.slug}
                  className={`flex items-center justify-between p-2 rounded-md cursor-pointer hover:bg-muted/30 transition-colors ${
                    selectedRepo?.slug === repo.slug ? 'border border-primary/50 bg-primary/5' : ''
                  }`}
                  onClick={() => handleRepoSelect(repo)}
                >
                  <div className="flex flex-col min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium truncate">{repo.name}</span>
                      <Badge variant={repo.is_private ? "secondary" : "outline"} className="text-xs">
                        {repo.is_private ? 'Private' : 'Public'}
                      </Badge>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No repositories found in this workspace.</p>
          )}
        </div>
      )}

      {selectedRepo && (
        <div>
          <label className="text-sm font-medium mb-1.5 block">Branch</label>
          {isLoadingBranches ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin" />
              Loading branches...
            </div>
          ) : branches.length > 0 ? (
            <Select value={selectedBranch} onValueChange={setSelectedBranch}>
              <SelectTrigger>
                <SelectValue placeholder="Select a branch..." />
              </SelectTrigger>
              <SelectContent>
                {branches.map((branch) => (
                  <SelectItem key={branch.name} value={branch.name}>
                    <div className="flex items-center gap-2">
                      <GitBranch className="w-3 h-3 text-muted-foreground" />
                      <span>{branch.name}</span>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <p className="text-xs text-muted-foreground">No branches found.</p>
          )}
        </div>
      )}

      {hasCompleteSelection && (
        <div className="flex items-center justify-between p-3 border border-border rounded-lg bg-muted/30">
          <div className="text-sm flex items-center gap-2">
            {selectionMatchesSaved && <Check className="w-4 h-4 text-green-500" />}
            <span className="font-medium">{selectedWorkspace}</span>
            <span className="text-muted-foreground">/</span>
            <span className="font-medium">{selectedRepo.name}</span>
            <span className="text-muted-foreground">/</span>
            <span className="font-medium">{selectedBranch}</span>
          </div>
          <div className="flex gap-2">
            {!selectionMatchesSaved && (
              <Button size="sm" onClick={handleSaveSelection}>
                Save
              </Button>
            )}
            <Button size="sm" variant="outline" onClick={handleClearSelection}>
              Clear
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

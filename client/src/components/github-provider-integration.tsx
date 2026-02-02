'use client';

import { useState, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, ExternalLink, LogOut, ChevronDown, ChevronRight, GitBranch, Folder, RefreshCw } from 'lucide-react';
import Image from 'next/image';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useGitHubStatus } from '@/hooks/use-github-status';
import { ToastAction } from "@/components/ui/toast";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL;

export interface GitHubCredentials {
  connected: boolean;
  username?: string;
}

export interface Repository {
  id: number;
  name: string;
  full_name: string;
  private: boolean;
  html_url: string;
  description: string | null;
  default_branch: string;
  updated_at: string;
  permissions: {
    admin: boolean;
    maintain: boolean;
    pull: boolean;
    push: boolean;
    triage: boolean;
  };
  owner: {
    login: string;
    avatar_url: string;
  };
}

export interface Branch {
  name: string;
  commit: {
    sha: string;
    url: string;
  };
  protected: boolean;
}

interface RepoSelectionResponse {
  repository: Repository;
  branch: Branch;
}

export class GitHubIntegrationService {
  private static getAuthHeaders(userId: string) {
    return {
      'X-User-ID': userId,
    };
  }

  static async checkStatus(userId: string): Promise<GitHubCredentials> {
    const response = await fetch(`${BACKEND_URL}/github/status`, {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      return { connected: false };
    }

    return response.json();
  }

  static async initiateOAuth(userId: string): Promise<string> {
    try {
      const response = await fetch(`${BACKEND_URL}/github/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId }),
      });

      if (!response.ok) {
        let errorData;
        try {
          errorData = await response.json();
        } catch {
          const errorText = await response.text();
          const error = new Error(errorText || 'Failed to initiate GitHub OAuth');
          (error as any).isHandled = true;
          throw error;
        }
        
        // Check for configuration error
        if (errorData?.error_code === 'GITHUB_NOT_CONFIGURED') {
          const configError = new Error(errorData.message || 'GitHub OAuth is not configured');
          (configError as any).errorCode = 'GITHUB_NOT_CONFIGURED';
          (configError as any).isHandled = true;
          throw configError;
        }
        
        const error = new Error(errorData?.message || errorData?.error || 'Failed to initiate GitHub OAuth');
        (error as any).isHandled = true;
        throw error;
      }

      const data = await response.json();
      if (!data?.oauth_url) {
        const error = new Error('GitHub OAuth URL was not returned by the server');
        (error as any).isHandled = true;
        throw error;
      }

      return data.oauth_url;
    } catch (error) {
      // Re-throw handled errors
      if ((error as any)?.isHandled) {
        throw error;
      }
      // Wrap unhandled errors
      const wrappedError = new Error(error instanceof Error ? error.message : 'Failed to initiate GitHub OAuth');
      (wrappedError as any).isHandled = true;
      throw wrappedError;
    }
  }

  static async disconnect(userId: string): Promise<void> {
    const response = await fetch(`${BACKEND_URL}/github/disconnect`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...this.getAuthHeaders(userId),
      },
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to disconnect GitHub');
    }
  }

  static async fetchRepositories(userId: string): Promise<any> {
    const response = await fetch(`${BACKEND_URL}/github/user-repos`, {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch repositories from backend');
    }

    return response.json();
  }

  static async fetchBranches(userId: string, repoFullName: string): Promise<any> {
    const response = await fetch(
      `${BACKEND_URL}/github/user-branches/${encodeURIComponent(repoFullName)}`,
      {
        headers: this.getAuthHeaders(userId),
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch branches from backend');
    }

    return response.json();
  }

  static async loadRepoSelection(userId: string): Promise<RepoSelectionResponse | null> {
    const response = await fetch(`${BACKEND_URL}/github/repo-selection`, {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      return null;
    }

    return response.json();
  }

  static async saveRepoSelection(userId: string, repository: Repository, branch: Branch): Promise<void> {
    const response = await fetch(`${BACKEND_URL}/github/repo-selection`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...this.getAuthHeaders(userId),
      },
      body: JSON.stringify({ repository, branch }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to save GitHub repository selection');
    }
  }

  static async clearRepoSelection(userId: string): Promise<void> {
    const response = await fetch(`${BACKEND_URL}/github/repo-selection`, {
      method: 'DELETE',
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to clear GitHub repository selection');
    }
  }
}

export default function GitHubProviderIntegration() {
  // Authentication state
  const [userId, setUserId] = useState<string | null>(null);
  const [credentials, setCredentials] = useState<GitHubCredentials>({ connected: false });
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  
  // Single source of truth for GitHub status
  const githubStatus = useGitHubStatus(userId);
  const [isLoading, setIsLoading] = useState(false);
  const { toast } = useToast();
  
  // Repository and branch state
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);
  const [selectedRepo, setSelectedRepo] = useState<Repository | null>(null);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [isLoadingBranches, setIsLoadingBranches] = useState(false);
  const [selectedBranch, setSelectedBranch] = useState<Branch | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [isGitHubSelected, setIsGitHubSelected] = useState(false);
  const [isLoadingStoredSelection, setIsLoadingStoredSelection] = useState(false);
  const [hasAttemptedLoad, setHasAttemptedLoad] = useState(false);
  const [hasShownSelectionPrompt, setHasShownSelectionPrompt] = useState(false);

  // Fetch user ID on component mount
  useEffect(() => {
    const fetchUserId = async () => {
      try {
        const response = await fetch('/api/getUserId');
        if (response.ok) {
          const data = await response.json();
          setUserId(data.userId);
        }
      } catch (error) {
        console.error('Error fetching user ID:', error);
      }
    };
    fetchUserId();
  }, []);


  // Check GitHub connection status
  useEffect(() => {
    if (userId) {
      checkGitHubStatus();
    }
  }, [userId]);

  // Sync local credentials with single source of truth
  useEffect(() => {
    setCredentials({ 
      connected: githubStatus.isAuthenticated,
      username: githubStatus.username 
    });
  }, [githubStatus.isAuthenticated, githubStatus.username]);

  // Fetch repositories when connected
  useEffect(() => {
    if (githubStatus.isAuthenticated && userId) {
      fetchRepositories();
    }
  }, [githubStatus.isAuthenticated, userId]);

  // Auto-expand repository list when connected but no repo is selected
  useEffect(() => {
    if (githubStatus.isAuthenticated && !githubStatus.isConnected && repositories.length > 0 && !selectedRepo && !isLoadingRepos && !isLoadingStoredSelection && !hasShownSelectionPrompt) {
      // Auto-expand to show repositories and prompt selection
      setExpanded(true);
      setHasShownSelectionPrompt(true);
      toast({
        title: "Select a Repository",
        description: "Please select a repository and branch to continue using GitHub integration.",
        variant: "default",
      });
    }
    // Reset the flag when a repo is selected
    if (selectedRepo) {
      setHasShownSelectionPrompt(false);
    }
  }, [githubStatus.isAuthenticated, githubStatus.isConnected, repositories.length, selectedRepo, isLoadingRepos, isLoadingStoredSelection, hasShownSelectionPrompt]);

  // Fetch repositories when dialog opens
  useEffect(() => {
    if (expanded && githubStatus.isAuthenticated && userId && repositories.length === 0 && !isLoadingRepos) {
      fetchRepositories();
    }
  }, [expanded, githubStatus.isAuthenticated, userId]);

  // Load stored selection AFTER repositories are fetched (only if no cached selection)
  useEffect(() => {
    if (githubStatus.isAuthenticated && userId && repositories.length > 0 && !selectedRepo && !hasAttemptedLoad) {
      console.log("Repositories loaded, now loading stored selection...");
      setHasAttemptedLoad(true);
      loadStoredSelection();
    }
  }, [repositories.length, githubStatus.isAuthenticated, userId, selectedRepo, hasAttemptedLoad]);
  
  // Debug logging for provider state - MUST be before any conditional returns
  useEffect(() => {
    const isSelected = selectedRepo && selectedBranch && isGitHubSelected;
    console.log("[GitHub Provider] State:", {
      isSelected,
      selectedRepo: selectedRepo?.name,
      selectedBranch: selectedBranch?.name,
      isGitHubSelected,
      isLoadingStoredSelection,
      credentialsConnected: githubStatus.isAuthenticated,
      isConnected: githubStatus.isConnected,
      hasRepos: repositories.length > 0
    });
  }, [selectedRepo, selectedBranch, isGitHubSelected, isLoadingStoredSelection, githubStatus.isAuthenticated, githubStatus.isConnected, repositories.length]);

  const checkGitHubStatus = async () => {
    console.log("checkGitHubStatus called, userId:", userId);
    if (!userId) return;
    
    setIsCheckingStatus(true);
    try {
      const data = await GitHubIntegrationService.checkStatus(userId);
      console.log("GitHub status data:", data);
      setCredentials(data);
      // Refresh the single source of truth
      githubStatus.refresh();
    } catch (error) {
      console.error("Error checking GitHub status:", error);
      setCredentials({ connected: false });
      githubStatus.refresh();
    } finally {
      setIsCheckingStatus(false);
    }
  };

  const fetchRepositories = async () => {
    console.log("fetchRepositories called, userId:", userId);
    if (!userId) {
      console.log("No userId, returning");
      return;
    }
    
    setIsLoadingRepos(true);
    try {
      const data = await GitHubIntegrationService.fetchRepositories(userId);
      const repos: Repository[] = Array.isArray(data)
        ? data
        : Array.isArray(data?.repos)
          ? data.repos
          : [];

      if (repos.length > 0) {
        console.log(`Found ${repos.length} repositories`);
        setRepositories(repos);
      } else {
        console.log("No repositories found or user not authenticated");
        setRepositories([]);
      }
    } catch (error) {
      console.error("Error fetching repositories:", error);
      setRepositories([]);
    } finally {
      setIsLoadingRepos(false);
    }
  };

  const fetchBranches = async (repoFullName: string, repo?: Repository) => {
    if (!userId) return;
    
    const targetRepo = repo || repositories.find(r => r.full_name === repoFullName);
    if (!targetRepo) {
      console.error("Repository not found for branch fetching:", repoFullName);
      return;
    }
    
    setIsLoadingBranches(true);
    try {
      const data = await GitHubIntegrationService.fetchBranches(userId, repoFullName);
      const branchList: Branch[] = Array.isArray(data?.branches) ? data.branches : [];

      if (branchList.length > 0) {
        setBranches(branchList);
        const defaultBranch = branchList.find(branch => branch.name === targetRepo.default_branch);
        if (defaultBranch) {
          console.log("Auto-selecting default branch:", defaultBranch.name, "for repo:", targetRepo.full_name);
          setSelectedBranch(defaultBranch);
          setIsGitHubSelected(true);
          console.log("Auto-saving default branch selection:", targetRepo.full_name, defaultBranch.name);
          saveRepoSelection(targetRepo, defaultBranch);
        } else {
          console.log("Auto-selecting first branch:", branchList[0].name, "for repo:", targetRepo.full_name);
          setSelectedBranch(branchList[0]);
          setIsGitHubSelected(true);
          console.log("Auto-saving first branch selection:", targetRepo.full_name, branchList[0].name);
          saveRepoSelection(targetRepo, branchList[0]);
        }
      } else {
        console.log("No branches found");
        setBranches([]);
      }
    } catch (error) {
      console.error("Error fetching branches:", error);
      setBranches([]);
    } finally {
      setIsLoadingBranches(false);
    }
  };

  const handleRepositorySelect = (repoFullName: string) => {
    console.log("handleRepositorySelect called with:", repoFullName);
    const repo = repositories.find(r => r.full_name === repoFullName);
    if (repo) {
      console.log("Found repo:", repo.full_name, "setting as selected");
      setSelectedRepo(repo);
      setSelectedBranch(null);
      setBranches([]);
      // Pass the repo object to avoid state timing issues
      fetchBranches(repoFullName, repo);
    } else {
      console.error("Repository not found:", repoFullName);
    }
  };

  const handleBranchSelect = (branchName: string) => {
    console.log("handleBranchSelect called with:", branchName);
    const branch = branches.find(b => b.name === branchName);
    if (branch) {
      console.log("Found branch:", branch.name, "for repo:", selectedRepo?.full_name);
      setSelectedBranch(branch);
      // Auto-select GitHub when repo and branch are chosen
      setIsGitHubSelected(true);
      // Save the selection to database
      if (selectedRepo) {
        console.log("Calling saveRepoSelection with:", selectedRepo.full_name, branch.name);
        saveRepoSelection(selectedRepo, branch);
      } else {
        console.error("No selectedRepo when trying to save branch selection");
      }
    } else {
      console.error("Branch not found:", branchName);
    }
  };

  const loadStoredSelection = async () => {
    if (!userId) return;
    
    console.log("[GitHub Provider] loadStoredSelection START");
    setIsLoadingStoredSelection(true);
    
    try {
      const data = await GitHubIntegrationService.loadRepoSelection(userId);
      if (data?.repository && data.branch) {
        console.log("Loading stored GitHub selection:", data.repository.full_name, data.branch.name);
        setSelectedRepo(data.repository);
        setSelectedBranch(data.branch);
        setIsGitHubSelected(true);
        fetchBranches(data.repository.full_name);
      }
    } catch (error) {
      console.error("Error loading stored GitHub selection:", error);
    } finally {
      console.log("[GitHub Provider] loadStoredSelection END");
      setIsLoadingStoredSelection(false);
    }
  };

  const saveRepoSelection = async (repository: Repository, branch: Branch) => {
    if (!userId) return;
    
    try {
      await GitHubIntegrationService.saveRepoSelection(userId, repository, branch);
      console.log("Saved GitHub repo selection:", repository.full_name, branch.name);
      // Refresh the single source of truth
      githubStatus.refresh();
      toast({
        title: "Repository Selected",
        description: `Selected ${repository.name} / ${branch.name}`,
      });
    } catch (error) {
      console.error("Error saving GitHub repo selection:", error);
    }
  };

  const clearRepoSelection = async () => {
    if (!userId) return;
    
    try {
      await GitHubIntegrationService.clearRepoSelection(userId);
      console.log("Cleared GitHub repo selection");
      setSelectedRepo(null);
      setSelectedBranch(null);
      setBranches([]);
      setIsGitHubSelected(false);
      setHasShownSelectionPrompt(false);
      // Refresh the single source of truth
      githubStatus.refresh();
    } catch (error) {
      console.error("Error clearing GitHub repo selection:", error);
    }
  };

  const handleOAuthLogin = async () => {
    if (!userId) {
      toast({
        title: "Error",
        description: "User ID is required",
        variant: "destructive",
      });
      return;
    }

    setIsLoading(true);

    try {
      // Get OAuth URL from backend
      const oauthUrl = await GitHubIntegrationService.initiateOAuth(userId);
      const popup = window.open(
        oauthUrl,
        'github-oauth',
        'width=600,height=700,scrollbars=yes,resizable=yes'
      );

      const checkClosed = setInterval(() => {
        if (popup?.closed) {
          clearInterval(checkClosed);
          setIsLoading(false);
          setTimeout(() => checkGitHubStatus(), 1000);
        }
      }, 1000);
    } catch (error: any) {
      // Prevent error from being logged as uncaught
      if (error.isHandled) {
        // Error is already marked as handled, just log for debugging
        console.log("OAuth error (handled):", error.message);
      } else {
        console.error("OAuth error:", error);
      }
      
      // Check if this is a configuration error
      if (error.errorCode === 'GITHUB_NOT_CONFIGURED') {
        const readmeAction = (
          <ToastAction
            altText="View GitHub setup guide"
            onClick={() => {
              window.open(
                "https://github.com/arvo-ai/aurora/blob/main/server/connectors/github_connector/README.md",
                "_blank",
                "noopener,noreferrer"
              );
            }}
            className="flex items-center gap-1"
          >
            <ExternalLink className="h-3 w-3" />
            View Setup Guide
          </ToastAction>
        );
        
        toast({
          title: "GitHub OAuth Not Configured",
          description: "GitHub OAuth environment variables are not configured. Please configure them to connect GitHub.",
          variant: "destructive",
          action: readmeAction,
        });
      } else {
        toast({
          title: "Connection Failed",
          description: error.message || "Failed to connect to GitHub",
          variant: "destructive",
        });
      }
      setIsLoading(false);
      
      // Prevent error from propagating and causing app crash
      return;
    }
  };

  const handleDisconnect = async () => {
    if (!userId) return;
    
    try {
      await GitHubIntegrationService.disconnect(userId);
      await clearRepoSelection();
      setCredentials({ connected: false });
      setRepositories([]);
      setSelectedRepo(null);
      setSelectedBranch(null);
      setBranches([]);
      setExpanded(false);
      setIsGitHubSelected(false);
      setHasShownSelectionPrompt(false);
      // Refresh the single source of truth
      githubStatus.refresh();
      // Notify other components to refresh their status
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({
        title: "Disconnected",
        description: "GitHub account disconnected successfully",
      });
    } catch (error) {
      console.error("Disconnect error:", error);
    }
  };

  // Loading state
  if (isCheckingStatus) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground p-3 border border-border rounded-lg">
        <Loader2 className="w-4 h-4 animate-spin" />
        Checking GitHub connection...
      </div>
    );
  }

  const isSelected = selectedRepo && selectedBranch && isGitHubSelected;

  return (
    <>
      <div
        className="flex items-center justify-between p-3 border border-border rounded-lg hover:bg-muted/50 transition-colors cursor-pointer"
        onClick={() => {
          if (!githubStatus.isAuthenticated) return;
          setExpanded(!expanded);
        }}
      >
        <div className="flex items-center gap-3">
          <div className="w-6 h-6 relative flex-shrink-0">
            <Image src="/github-mark.svg" alt="GitHub" fill className="object-contain" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <p className={`${!githubStatus.isConnected && !githubStatus.isAuthenticated ? 'text-muted-foreground' : ''} font-medium truncate`}>GitHub</p>
              {githubStatus.isConnected && (
                <Badge variant="default" className="text-xs bg-primary text-primary-foreground">Selected</Badge>
              )}
              {githubStatus.isAuthenticated && !githubStatus.isConnected && (
                <Badge variant="outline" className="text-xs border-yellow-500 text-yellow-600">Available</Badge>
              )}
            </div>
            <div className="flex items-center gap-1 mt-1">
              {githubStatus.isConnected ? (
                <div className="flex items-center gap-1">
                  <Check className="w-3 h-3 text-green-500" />
                  <span className="text-xs text-muted-foreground">Connected</span>
                  <span className="text-xs text-muted-foreground">•</span>
                  <span className="text-xs text-muted-foreground">{selectedRepo?.name}/{selectedBranch?.name}</span>
                </div>
              ) : githubStatus.isAuthenticated ? (
                isLoadingStoredSelection ? (
                  <div className="flex items-center gap-1">
                    <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
                    <span className="text-xs text-muted-foreground">Loading selection...</span>
                  </div>
                ) : (
                  <div className="flex items-center gap-1">
                    <div className="w-3 h-3 rounded-full bg-yellow-500 flex items-center justify-center">
                      <span className="text-white text-xs font-bold leading-none">!</span>
                    </div>
                    <span className="text-xs text-yellow-600 font-medium">Available - Select repository</span>
                  </div>
                )
              ) : (
                <span className="text-xs text-muted-foreground">Not connected</span>
              )}
            </div>
          </div>
        </div>
        
        {/* Arrow icon */}
        <div className="flex items-center gap-2 flex-shrink-0">
          {/* Selection checkbox */}
          {githubStatus.isConnected && (
            <div 
              className="flex items-center justify-center w-5 h-5 rounded border-2 border-border hover:border-primary transition-colors cursor-pointer"
              onClick={(e) => {
                e.stopPropagation();
                // Toggle GitHub selection
                setIsGitHubSelected(!isGitHubSelected);
              }}
            >
              {isSelected && (
                <Check className="w-3 h-3 text-primary" />
              )}
            </div>
          )}
          
          {/* Provider buttons */}
          {githubStatus.isAuthenticated ? (
            <div className="flex gap-2">
              <Button 
                variant="ghost" 
                size="sm" 
                className="px-3" 
                onClick={(e) => {
                  e.stopPropagation();
                  // Refresh - check status and fetch repos
                  checkGitHubStatus();
                  fetchRepositories();
                }}
                title="Refresh repositories"
              >
                <RefreshCw className="h-4 w-4" />
              </Button>
              <Button 
                variant="ghost" 
                size="sm" 
                className="px-3 text-red-600 hover:text-red-700 hover:bg-red-50" 
                onClick={(e) => { 
                  e.stopPropagation(); 
                  handleDisconnect();
                }}
                title="Disconnect GitHub"
              >
                <LogOut className="h-4 w-4" />
              </Button>
            </div>
          ) : (
            <Button
              variant="outline"
              size="sm"
              onClick={(e) => {
                e.stopPropagation();
                handleOAuthLogin();
              }}
              disabled={isLoading || !userId}
              className="w-24 bg-white text-black hover:bg-gray-50 border-gray-300"
            >
              {isLoading ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                "Connect"
              )}
            </Button>
          )}
          
          {/* Dropdown arrow */}
          {githubStatus.isAuthenticated && (
            <div 
              className="cursor-pointer p-1 hover:bg-muted rounded"
              onClick={(e) => {
                e.stopPropagation();
                setExpanded(!expanded);
              }}
            >
              {expanded ? <ChevronDown className="w-4 h-4 text-muted-foreground"/> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
            </div>
          )}
        </div>
      </div>
      
      {/* Expanded repository/branch list */}
      {expanded && githubStatus.isAuthenticated && (
        <div className="ml-6 border-l-2 border-muted pl-6 mt-2 space-y-3 max-h-60 overflow-y-auto">
          {/* Repositories header with count */}
          <div className="flex items-center justify-between mb-1">
            <div className="flex items-center gap-2">
              <p className="text-sm font-medium text-muted-foreground">Repositories</p>
              {repositories.length > 0 && (
                <Badge variant="outline" className="text-xs">
                  {repositories.length} available
                </Badge>
              )}
            </div>
          </div>
          
          {/* Repository items */}
          {isLoadingRepos ? (
            <p className="text-xs text-muted-foreground">Loading repositories…</p>
          ) : repositories.length > 0 ? (
            repositories.map(repo => (
              <div 
                key={repo.id} 
                className={`flex items-center justify-between p-2 rounded-md border cursor-pointer hover:bg-muted/30 transition-colors ${
                  selectedRepo?.id === repo.id ? 'border-primary/50 bg-primary/5' : 'border-border'
                }`}
                onClick={() => handleRepositorySelect(repo.full_name)}
              >
                <div className="flex flex-col flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium truncate">{repo.name}</span>
                    {repo.private && (
                      <Badge variant="secondary" className="text-xs">Private</Badge>
                    )}
                  </div>
                  {repo.name !== repo.full_name && (
                    <span className="text-xs text-muted-foreground truncate">{repo.full_name}</span>
                  )}
                  {selectedRepo?.id === repo.id && selectedBranch && (
                    <div className="flex items-center gap-1 mt-1">
                      <GitBranch className="w-3 h-3 text-muted-foreground" />
                      <span className="text-xs text-muted-foreground">{selectedBranch.name}</span>
                      {selectedBranch.name === repo.default_branch && (
                        <Badge variant="secondary" className="text-xs ml-1">Default</Badge>
                      )}
                    </div>
                  )}
                </div>
                
                {/* Branch selector for selected repo */}
                {selectedRepo?.id === repo.id && (
                  <div className="ml-3 min-w-0 flex-shrink-0" onClick={(e) => e.stopPropagation()}>
                    {isLoadingBranches ? (
                      <div className="flex items-center gap-2">
                        <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
                        <span className="text-xs text-muted-foreground">Loading...</span>
                      </div>
                    ) : branches.length > 0 ? (
                      <Select onValueChange={handleBranchSelect} value={selectedBranch?.name || ""}>
                        <SelectTrigger className="w-40 h-8 text-xs">
                          <SelectValue placeholder="Select branch..." />
                        </SelectTrigger>
                        <SelectContent className="z-[300] w-48">
                          {branches.map((branch) => (
                            <SelectItem key={branch.name} value={branch.name} className="w-full">
                              <div className="flex items-center gap-2 w-full min-w-0">
                                <GitBranch className="w-3 h-3 text-muted-foreground flex-shrink-0" />
                                <span className="text-xs truncate flex-1">{branch.name}</span>
                                {branch.name === repo.default_branch && (
                                  <Badge variant="secondary" className="text-xs flex-shrink-0 ml-1">Default</Badge>
                                )}
                              </div>
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <span className="text-xs text-muted-foreground">No branches</span>
                    )}
                  </div>
                )}
              </div>
            ))
          ) : (
            <div className="text-xs text-muted-foreground text-center py-4">
              No repositories available. Try refreshing or reconnect your account.
            </div>
          )}
        </div>
      )}
    </>
  );
}
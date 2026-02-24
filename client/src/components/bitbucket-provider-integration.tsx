'use client';

import { useState, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, LogOut, GitBranch, RefreshCw } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { getEnv } from '@/lib/env';

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

const REQUIRED_API_TOKEN_SCOPES = [
  'read:user:bitbucket',
  'read:workspace:bitbucket',
  'read:project:bitbucket',
  'read:repository:bitbucket',
  'read:pullrequest:bitbucket',
  'read:issue:bitbucket',
] as const;

export class BitbucketIntegrationService {
  private static getAuthHeaders(userId: string) {
    return { 'X-User-ID': userId };
  }

  static async checkStatus(userId: string) {
    const response = await fetch(`${BACKEND_URL}/bitbucket/status`, {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      return { connected: false };
    }

    return response.json();
  }

  static async initiateOAuth(userId: string): Promise<string> {
    const response = await fetch(`${BACKEND_URL}/bitbucket/login`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...this.getAuthHeaders(userId),
      },
      body: JSON.stringify({}),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to initiate Bitbucket OAuth');
    }

    const data = await response.json();
    if (!data?.oauth_url) {
      throw new Error('Bitbucket OAuth URL was not returned by the server');
    }

    return data.oauth_url;
  }

  static async connectWithApiToken(userId: string, email: string, apiToken: string) {
    const response = await fetch(`${BACKEND_URL}/bitbucket/login`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...this.getAuthHeaders(userId),
      },
      body: JSON.stringify({ api_token: apiToken, email }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to connect with API token');
    }

    return response.json();
  }

  static async disconnect(userId: string): Promise<void> {
    const response = await fetch(`${BACKEND_URL}/bitbucket/disconnect`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...this.getAuthHeaders(userId),
      },
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to disconnect Bitbucket');
    }
  }

  static async getWorkspaces(userId: string) {
    const response = await fetch(`${BACKEND_URL}/bitbucket/workspaces`, {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch workspaces');
    }

    return response.json();
  }

  static async getProjects(userId: string, workspace: string) {
    const response = await fetch(`${BACKEND_URL}/bitbucket/projects/${encodeURIComponent(workspace)}`, {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch projects');
    }

    return response.json();
  }

  static async getRepos(userId: string, workspace: string, project?: string) {
    const url = new URL(`${BACKEND_URL}/bitbucket/repos/${encodeURIComponent(workspace)}`);
    if (project) url.searchParams.set('project', project);

    const response = await fetch(url.toString(), {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch repositories');
    }

    return response.json();
  }

  static async getBranches(userId: string, workspace: string, repoSlug: string) {
    const response = await fetch(
      `${BACKEND_URL}/bitbucket/branches/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`,
      { headers: this.getAuthHeaders(userId) }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch branches');
    }

    return response.json();
  }

  static async getPullRequests(userId: string, workspace: string, repoSlug: string, state?: string) {
    const url = new URL(`${BACKEND_URL}/bitbucket/pull-requests/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`);
    if (state) url.searchParams.set('state', state);

    const response = await fetch(url.toString(), {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch pull requests');
    }

    return response.json();
  }

  static async getIssues(userId: string, workspace: string, repoSlug: string) {
    const response = await fetch(
      `${BACKEND_URL}/bitbucket/issues/${encodeURIComponent(workspace)}/${encodeURIComponent(repoSlug)}`,
      { headers: this.getAuthHeaders(userId) }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to fetch issues');
    }

    return response.json();
  }

  static async loadWorkspaceSelection(userId: string) {
    const response = await fetch(`${BACKEND_URL}/bitbucket/workspace-selection`, {
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      return null;
    }

    return response.json();
  }

  static async saveWorkspaceSelection(userId: string, data: any) {
    const response = await fetch(`${BACKEND_URL}/bitbucket/workspace-selection`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...this.getAuthHeaders(userId),
      },
      body: JSON.stringify(data),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to save workspace selection');
    }

    return response.json();
  }

  static async clearWorkspaceSelection(userId: string) {
    const response = await fetch(`${BACKEND_URL}/bitbucket/workspace-selection`, {
      method: 'DELETE',
      headers: this.getAuthHeaders(userId),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Failed to clear workspace selection');
    }
  }
}

interface Workspace {
  slug: string;
  name: string;
  uuid: string;
}

interface Repo {
  slug: string;
  name: string;
  full_name: string;
  is_private: boolean;
  description?: string;
  mainbranch?: { name: string };
}

interface Branch {
  name: string;
}

export default function BitbucketProviderIntegration() {
  const [userId, setUserId] = useState<string | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);
  const [displayName, setDisplayName] = useState<string>('');
  const [authType, setAuthType] = useState<string>('');
  const [isLoading, setIsLoading] = useState(false);
  const { toast } = useToast();

  // API token form
  const [email, setEmail] = useState('');
  const [apiToken, setApiToken] = useState('');

  // Workspace browser state
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string>('');
  const [isLoadingWorkspaces, setIsLoadingWorkspaces] = useState(false);

  const [repos, setRepos] = useState<Repo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<Repo | null>(null);
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);

  const [branches, setBranches] = useState<Branch[]>([]);
  const [selectedBranch, setSelectedBranch] = useState<string>('');
  const [isLoadingBranches, setIsLoadingBranches] = useState(false);

  // Fetch user ID on mount
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

  // Check connection status
  useEffect(() => {
    if (userId) {
      checkStatus();
    }
  }, [userId]);

  // Fetch workspaces when authenticated
  useEffect(() => {
    if (isAuthenticated && userId) {
      fetchWorkspaces();
      loadStoredSelection();
    }
  }, [isAuthenticated, userId]);

  // Fetch repos when workspace changes
  useEffect(() => {
    if (isAuthenticated && userId && selectedWorkspace) {
      fetchRepos(selectedWorkspace);
    }
  }, [selectedWorkspace, isAuthenticated, userId]);

  const checkStatus = async () => {
    if (!userId) return;
    setIsCheckingStatus(true);
    try {
      const data = await BitbucketIntegrationService.checkStatus(userId);
      setIsAuthenticated(data.connected || false);
      setDisplayName(data.display_name || data.username || '');
      setAuthType(data.auth_type || '');
    } catch (error) {
      console.error('Error checking Bitbucket status:', error);
      setIsAuthenticated(false);
    } finally {
      setIsCheckingStatus(false);
    }
  };

  const handleOAuthLogin = async () => {
    if (!userId) {
      toast({ title: "Error", description: "User ID is required", variant: "destructive" });
      return;
    }
    setIsLoading(true);
    try {
      const oauthUrl = await BitbucketIntegrationService.initiateOAuth(userId);
      const popup = window.open(oauthUrl, 'bitbucket-oauth', 'width=600,height=700,scrollbars=yes,resizable=yes');

      const checkClosed = setInterval(() => {
        if (popup?.closed) {
          clearInterval(checkClosed);
          setIsLoading(false);
          setTimeout(() => {
            checkStatus();
            window.dispatchEvent(new CustomEvent('providerStateChanged'));
          }, 1000);
        }
      }, 1000);
    } catch (error: any) {
      console.error('Bitbucket OAuth error:', error);
      toast({ title: "Connection Failed", description: error.message || "Failed to connect to Bitbucket", variant: "destructive" });
      setIsLoading(false);
    }
  };

  const handleApiTokenLogin = async () => {
    if (!userId) {
      toast({ title: "Error", description: "User ID is required", variant: "destructive" });
      return;
    }
    if (!email || !apiToken) {
      toast({ title: "Error", description: "Email and API token are required", variant: "destructive" });
      return;
    }
    setIsLoading(true);
    try {
      await BitbucketIntegrationService.connectWithApiToken(userId, email, apiToken);
      toast({ title: "Connected", description: "Bitbucket connected with API token" });
      setEmail('');
      setApiToken('');
      checkStatus();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
    } catch (error: any) {
      console.error('API token login error:', error);
      toast({ title: "Connection Failed", description: error.message || "Failed to connect with API token", variant: "destructive" });
    } finally {
      setIsLoading(false);
    }
  };

  const handleDisconnect = async () => {
    if (!userId) return;
    try {
      await BitbucketIntegrationService.disconnect(userId);
      setIsAuthenticated(false);
      setDisplayName('');
      setAuthType('');
      setWorkspaces([]);
      setSelectedWorkspace('');
      setRepos([]);
      setSelectedRepo(null);
      setBranches([]);
      setSelectedBranch('');
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Disconnected", description: "Bitbucket account disconnected successfully" });
    } catch (error: any) {
      console.error('Disconnect error:', error);
      toast({ title: "Error", description: error.message || "Failed to disconnect", variant: "destructive" });
    }
  };

  const fetchWorkspaces = async () => {
    if (!userId) return;
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
    if (!userId) return;
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
    if (!userId) return;
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
    if (!userId) return;
    try {
      const data = await BitbucketIntegrationService.loadWorkspaceSelection(userId);
      if (data?.workspace) {
        setSelectedWorkspace(data.workspace);
        if (data.repository) {
          setSelectedRepo(data.repository);
          if (data.branch) {
            setSelectedBranch(data.branch);
          }
        }
      }
    } catch (error) {
      console.error('Error loading stored selection:', error);
    }
  };

  const handleSaveSelection = async () => {
    if (!userId || !selectedWorkspace || !selectedRepo || !selectedBranch) {
      toast({ title: "Error", description: "Please select a workspace, repository, and branch", variant: "destructive" });
      return;
    }
    try {
      await BitbucketIntegrationService.saveWorkspaceSelection(userId, {
        workspace: selectedWorkspace,
        repository: selectedRepo,
        branch: selectedBranch,
      });
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Selection Saved", description: `${selectedWorkspace} / ${selectedRepo.name} / ${selectedBranch}` });
    } catch (error: any) {
      console.error('Error saving selection:', error);
      toast({ title: "Error", description: error.message || "Failed to save selection", variant: "destructive" });
    }
  };

  const handleClearSelection = async () => {
    if (!userId) return;
    try {
      await BitbucketIntegrationService.clearWorkspaceSelection(userId);
      setSelectedWorkspace('');
      setSelectedRepo(null);
      setBranches([]);
      setSelectedBranch('');
      setRepos([]);
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Selection Cleared", description: "Bitbucket workspace selection cleared" });
    } catch (error: any) {
      console.error('Error clearing selection:', error);
      toast({ title: "Error", description: error.message || "Failed to clear selection", variant: "destructive" });
    }
  };

  if (isCheckingStatus) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground p-3 border border-border rounded-lg">
        <Loader2 className="w-4 h-4 animate-spin" />
        Checking Bitbucket connection...
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Auth section */}
      {!isAuthenticated ? (
        <Tabs defaultValue="oauth" className="w-full">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="oauth">OAuth</TabsTrigger>
            <TabsTrigger value="api-token">API Token</TabsTrigger>
          </TabsList>
          <TabsContent value="oauth" className="space-y-3 mt-3">
            <p className="text-sm text-muted-foreground">
              Connect your Bitbucket Cloud account using OAuth.
            </p>
            <Button onClick={handleOAuthLogin} disabled={isLoading || !userId} className="w-full">
              {isLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Connecting...
                </>
              ) : (
                "Connect with Bitbucket"
              )}
            </Button>
          </TabsContent>
          <TabsContent value="api-token" className="space-y-3 mt-3">
            <p className="text-sm text-muted-foreground">
              Connect using a Bitbucket API token. Click{' '}
              <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noopener noreferrer" className="underline">
                &quot;Create API token with scopes&quot;
              </a>{' '}
              and grant these scopes:
            </p>
            <div className="text-xs bg-muted rounded-md p-2.5 space-y-0.5 font-mono">
              {REQUIRED_API_TOKEN_SCOPES.map((scope) => (
                <div key={scope}>{scope}</div>
              ))}
            </div>
            <Input
              type="email"
              placeholder="Bitbucket email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <Input
              type="password"
              placeholder="API token"
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
            />
            <Button onClick={handleApiTokenLogin} disabled={isLoading || !userId || !email || !apiToken} className="w-full">
              {isLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Connecting...
                </>
              ) : (
                "Connect"
              )}
            </Button>
          </TabsContent>
        </Tabs>
      ) : (
        <div className="flex items-center justify-between p-3 border border-border rounded-lg">
          <div className="flex items-center gap-3">
            <div className="relative h-6 w-6">
              <img src="/bitbucket.svg" alt="Bitbucket" className="h-6 w-6 object-contain" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-medium text-sm">{displayName || 'Bitbucket'}</span>
                {authType && (
                  <Badge variant="outline" className="text-xs">{authType}</Badge>
                )}
              </div>
              <div className="flex items-center gap-1 mt-0.5">
                <Check className="w-3 h-3 text-green-500" />
                <span className="text-xs text-muted-foreground">Connected</span>
              </div>
            </div>
          </div>
          <div className="flex gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => { checkStatus(); fetchWorkspaces(); }}
              title="Refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="text-red-600 hover:text-red-700 hover:bg-red-50"
              onClick={handleDisconnect}
              title="Disconnect Bitbucket"
            >
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      {/* Workspace browser */}
      {isAuthenticated && (
        <div className="space-y-3">
          {/* Workspace selector */}
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

          {/* Repo list */}
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
                          {repo.is_private ? (
                            <Badge variant="secondary" className="text-xs">Private</Badge>
                          ) : (
                            <Badge variant="outline" className="text-xs">Public</Badge>
                          )}
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

          {/* Branch selector */}
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

          {/* Selection summary and actions */}
          {selectedWorkspace && selectedRepo && selectedBranch && (
            <div className="flex items-center justify-between p-3 border border-border rounded-lg bg-muted/30">
              <div className="text-sm">
                <span className="font-medium">{selectedWorkspace}</span>
                <span className="text-muted-foreground"> / </span>
                <span className="font-medium">{selectedRepo.name}</span>
                <span className="text-muted-foreground"> / </span>
                <span className="font-medium">{selectedBranch}</span>
              </div>
              <div className="flex gap-2">
                <Button size="sm" onClick={handleSaveSelection}>
                  Save
                </Button>
                <Button size="sm" variant="outline" onClick={handleClearSelection}>
                  Clear
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

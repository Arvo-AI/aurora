'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, ExternalLink, LogOut, ChevronDown, ChevronRight, RefreshCw, Pencil, X, Search, RotateCw } from 'lucide-react';
import Image from 'next/image';
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useGitHubStatus } from '@/hooks/use-github-status';
import { ToastAction } from "@/components/ui/toast";
import { getEnv } from '@/lib/env';

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

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

export interface ConnectedRepo {
  repo_full_name: string;
  repo_id: number;
  default_branch: string;
  is_private: boolean;
  metadata_summary: string | null;
  metadata_status: string;
  repo_data: Repository | null;
  created_at: string | null;
}

export class GitHubIntegrationService {
  private static getAuthHeaders(userId: string) {
    return { 'X-User-ID': userId };
  }

  static async checkStatus(userId: string): Promise<GitHubCredentials> {
    const response = await fetch(`${BACKEND_URL}/github/status`, {
      headers: this.getAuthHeaders(userId),
    });
    if (!response.ok) return { connected: false };
    return response.json();
  }

  static async initiateOAuth(userId: string): Promise<string> {
    const response = await fetch(`${BACKEND_URL}/github/login`, {
      method: 'POST',
      headers: { ...this.getAuthHeaders(userId), 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId }),
    });
    if (!response.ok) {
      const errorData = await response.json().catch(() => null);
      if (errorData?.error_code === 'GITHUB_NOT_CONFIGURED') {
        const err = new Error(errorData.message || 'GitHub OAuth is not configured');
        (err as Error & { errorCode?: string; isHandled?: boolean }).errorCode = 'GITHUB_NOT_CONFIGURED';
        (err as Error & { isHandled?: boolean }).isHandled = true;
        throw err;
      }
      throw new Error('Failed to initiate GitHub OAuth');
    }
    const data = await response.json();
    return data.oauth_url;
  }

  static async disconnect(userId: string): Promise<void> {
    await fetch(`${BACKEND_URL}/github/disconnect`, {
      method: 'POST',
      headers: this.getAuthHeaders(userId),
    });
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  static async fetchRepositories(userId: string): Promise<any> {
    const response = await fetch(`${BACKEND_URL}/github/user-repos`, {
      headers: this.getAuthHeaders(userId),
    });
    if (!response.ok) throw new Error('Failed to fetch repositories');
    return response.json();
  }

  static async fetchRepoSelections(userId: string): Promise<ConnectedRepo[]> {
    const response = await fetch(`${BACKEND_URL}/github/repo-selections`, {
      headers: this.getAuthHeaders(userId),
    });
    if (!response.ok) return [];
    const data = await response.json();
    return data.repositories || [];
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  static async saveRepoSelections(userId: string, repositories: Repository[]): Promise<any> {
    const response = await fetch(`${BACKEND_URL}/github/repo-selections`, {
      method: 'POST',
      headers: { ...this.getAuthHeaders(userId), 'Content-Type': 'application/json' },
      body: JSON.stringify({ repositories }),
    });
    if (!response.ok) throw new Error('Failed to save repository selections');
    return response.json();
  }

  static async clearRepoSelections(userId: string): Promise<void> {
    await fetch(`${BACKEND_URL}/github/repo-selections`, {
      method: 'DELETE',
      headers: this.getAuthHeaders(userId),
    });
  }

  static async updateRepoMetadata(userId: string, repoFullName: string, summary: string): Promise<void> {
    const response = await fetch(`${BACKEND_URL}/github/repo-selections/${encodeURIComponent(repoFullName)}/metadata`, {
      method: 'PUT',
      headers: { ...this.getAuthHeaders(userId), 'Content-Type': 'application/json' },
      body: JSON.stringify({ metadata_summary: summary }),
    });
    if (!response.ok) throw new Error('Failed to update metadata');
  }

  static async generateRepoMetadata(userId: string, repoFullName: string): Promise<void> {
    const response = await fetch(`${BACKEND_URL}/github/repo-metadata/generate`, {
      method: 'POST',
      headers: { ...this.getAuthHeaders(userId), 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_full_name: repoFullName }),
    });
    if (!response.ok) throw new Error('Failed to trigger metadata generation');
  }
}

export default function GitHubProviderIntegration() {
  const [userId, setUserId] = useState<string | null>(null);
  const githubStatus = useGitHubStatus(userId);
  const [isLoading, setIsLoading] = useState(false);
  const { toast } = useToast();

  // Repo picker state
  const [allRepos, setAllRepos] = useState<Repository[]>([]);
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);
  const [checkedRepos, setCheckedRepos] = useState<Set<string>>(new Set());
  const [searchFilter, setSearchFilter] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  // Saved repos with metadata
  const [savedRepos, setSavedRepos] = useState<ConnectedRepo[]>([]);
  const [editingMetadata, setEditingMetadata] = useState<Record<string, string>>({});
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch('/api/getUserId').then(r => r.ok ? r.json() : null).then(d => {
      if (d?.userId) setUserId(d.userId);
    }).catch(() => {});
  }, []);

  const fetchAllRepos = useCallback(async () => {
    if (!userId) return;
    setIsLoadingRepos(true);
    try {
      const data = await GitHubIntegrationService.fetchRepositories(userId);
      const repos: Repository[] = Array.isArray(data) ? data : data?.repos || [];
      setAllRepos(repos);
    } catch { setAllRepos([]); }
    finally { setIsLoadingRepos(false); }
  }, [userId]);

  const startMetadataPolling = useCallback((repos: ConnectedRepo[]) => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    const hasPending = repos.some(r => r.metadata_status === 'pending' || r.metadata_status === 'generating');
    if (!hasPending || !userId) return;
    pollingRef.current = setInterval(async () => {
      try {
        const updated = await GitHubIntegrationService.fetchRepoSelections(userId!);
        setSavedRepos(updated);
        const stillPending = updated.some(r => r.metadata_status === 'pending' || r.metadata_status === 'generating');
        if (!stillPending && pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
      } catch {}
    }, 3000);
  }, [userId]);

  const loadSavedRepos = useCallback(async () => {
    if (!userId) return;
    try {
      const repos = await GitHubIntegrationService.fetchRepoSelections(userId);
      setSavedRepos(repos);
      setCheckedRepos(new Set(repos.map(r => r.repo_full_name)));
      startMetadataPolling(repos);
    } catch { setSavedRepos([]); }
  }, [userId, startMetadataPolling]);

  // Load repos and saved selections when authenticated
  useEffect(() => {
    if (!githubStatus.isAuthenticated || !userId) return;
    fetchAllRepos();
    loadSavedRepos();
  }, [githubStatus.isAuthenticated, userId, fetchAllRepos, loadSavedRepos]);

  useEffect(() => () => { if (pollingRef.current) clearInterval(pollingRef.current); }, []);

  const handleSaveSelections = async () => {
    if (!userId) return;
    setIsSaving(true);
    try {
      const selected = allRepos.filter(r => checkedRepos.has(r.full_name));
      await GitHubIntegrationService.saveRepoSelections(userId, selected);
      toast({ title: "Repositories saved", description: `${selected.length} repositories connected.` });
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      await loadSavedRepos();
    } catch {
      toast({ title: "Error", description: "Failed to save repositories", variant: "destructive" });
    } finally { setIsSaving(false); }
  };

  const handleMetadataSave = async (repoFullName: string) => {
    if (!userId) return;
    const summary = editingMetadata[repoFullName];
    if (summary === undefined) return;
    try {
      await GitHubIntegrationService.updateRepoMetadata(userId, repoFullName, summary);
      setSavedRepos(prev => prev.map(r =>
        r.repo_full_name === repoFullName ? { ...r, metadata_summary: summary, metadata_status: 'ready' } : r
      ));
      setEditingMetadata(prev => { const n = { ...prev }; delete n[repoFullName]; return n; });
      toast({ title: "Description updated" });
    } catch {
      toast({ title: "Error", description: "Failed to update description", variant: "destructive" });
    }
  };

  const handleRegenerate = async (repoFullName: string) => {
    if (!userId) return;
    try {
      await GitHubIntegrationService.generateRepoMetadata(userId, repoFullName);
      setSavedRepos(prev => prev.map(r =>
        r.repo_full_name === repoFullName ? { ...r, metadata_status: 'generating' } : r
      ));
      startMetadataPolling(savedRepos);
    } catch {
      toast({ title: "Error", description: "Failed to regenerate description", variant: "destructive" });
    }
  };

  const handleOAuthLogin = async () => {
    if (!userId) return;
    setIsLoading(true);
    try {
      const oauthUrl = await GitHubIntegrationService.initiateOAuth(userId);
      const popup = window.open(oauthUrl, 'github-oauth', 'width=600,height=700,scrollbars=yes,resizable=yes');
      const checkClosed = setInterval(() => {
        if (popup?.closed) {
          clearInterval(checkClosed);
          setIsLoading(false);
          setTimeout(() => githubStatus.refresh(), 1000);
        }
      }, 1000);
    } catch (error: unknown) {
      const err = error as Error & { errorCode?: string };
      if (err.errorCode === 'GITHUB_NOT_CONFIGURED') {
        toast({
          title: "GitHub OAuth Not Configured",
          description: "GitHub OAuth environment variables are not configured.",
          variant: "destructive",
          action: (
            <ToastAction altText="View setup guide"
              onClick={() => window.open("https://github.com/arvo-ai/aurora/blob/main/server/connectors/github_connector/README.md", "_blank")}>
              <ExternalLink className="h-3 w-3 mr-1" />Setup Guide
            </ToastAction>
          ),
        });
      } else {
        toast({ title: "Connection Failed", description: err.message || "Failed to connect to GitHub", variant: "destructive" });
      }
      setIsLoading(false);
    }
  };

  const handleDisconnect = async () => {
    if (!userId) return;
    try {
      await GitHubIntegrationService.clearRepoSelections(userId);
      await GitHubIntegrationService.disconnect(userId);
      setSavedRepos([]);
      setCheckedRepos(new Set());
      setAllRepos([]);
      setExpanded(false);
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Disconnected", description: "GitHub account disconnected" });
    } catch {}
  };

  if (githubStatus.hasReposConnected === null && !userId) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground p-3 border border-border rounded-lg">
        <Loader2 className="w-4 h-4 animate-spin" />Checking GitHub connection...
      </div>
    );
  }

  const filteredRepos = allRepos.filter(r =>
    r.full_name.toLowerCase().includes(searchFilter.toLowerCase())
  );

  const hasUnsavedChanges = (() => {
    const savedSet = new Set(savedRepos.map(r => r.repo_full_name));
    if (checkedRepos.size !== savedSet.size) return true;
    for (const name of checkedRepos) if (!savedSet.has(name)) return true;
    return false;
  })();

  return (
    <>
      {/* Header */}
      <div
        className="flex items-center justify-between p-3 border border-border rounded-lg hover:bg-muted/50 transition-colors cursor-pointer"
        onClick={() => { if (githubStatus.isAuthenticated) setExpanded(!expanded); }}
      >
        <div className="flex items-center gap-3">
          <div className="w-6 h-6 relative flex-shrink-0">
            <Image src="/github-mark.svg" alt="GitHub" fill className="object-contain" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <p className={`${!githubStatus.isConnected && !githubStatus.isAuthenticated ? 'text-muted-foreground' : ''} font-medium truncate`}>GitHub</p>
              {githubStatus.isConnected && <Badge variant="default" className="text-xs bg-primary text-primary-foreground">Connected</Badge>}
              {githubStatus.isAuthenticated && !githubStatus.isConnected && <Badge variant="outline" className="text-xs border-yellow-500 text-yellow-600">Available</Badge>}
            </div>
            <div className="flex items-center gap-1 mt-1">
              {githubStatus.isConnected ? (
                <div className="flex items-center gap-1">
                  <Check className="w-3 h-3 text-green-500" />
                  <span className="text-xs text-muted-foreground">{savedRepos.length} repo{savedRepos.length !== 1 ? 's' : ''} connected</span>
                </div>
              ) : githubStatus.isAuthenticated ? (
                <span className="text-xs text-yellow-600 font-medium">Select repositories to connect</span>
              ) : (
                <span className="text-xs text-muted-foreground">Not connected</span>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {githubStatus.isAuthenticated ? (
            <div className="flex gap-2">
              <Button variant="ghost" size="sm" className="px-3" onClick={(e) => { e.stopPropagation(); fetchAllRepos(); loadSavedRepos(); }} title="Refresh">
                <RefreshCw className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="sm" className="px-3 text-red-600 hover:text-red-700 hover:bg-red-50" onClick={(e) => { e.stopPropagation(); handleDisconnect(); }} title="Disconnect">
                <LogOut className="h-4 w-4" />
              </Button>
            </div>
          ) : (
            <Button variant="outline" size="sm" onClick={(e) => { e.stopPropagation(); handleOAuthLogin(); }} disabled={isLoading || !userId} className="w-24 bg-white text-black hover:bg-gray-50 border-gray-300">
              {isLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : "Connect"}
            </Button>
          )}
          {githubStatus.isAuthenticated && (
            <div className="cursor-pointer p-1 hover:bg-muted rounded" onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}>
              {expanded ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
            </div>
          )}
        </div>
      </div>

      {/* Expanded content */}
      {expanded && githubStatus.isAuthenticated && (
        <div className="ml-6 border-l-2 border-muted pl-6 mt-2 space-y-3">
          {/* Saved repos with metadata */}
          {savedRepos.length > 0 && (
            <div className="space-y-2">
              <p className="text-sm font-medium text-muted-foreground">Connected Repositories</p>
              {savedRepos.map(repo => (
                <div key={repo.repo_full_name} className="p-2 rounded-md border border-border space-y-1">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{repo.repo_full_name}</span>
                      {repo.is_private && <Badge variant="secondary" className="text-xs">Private</Badge>}
                    </div>
                    <div className="flex items-center gap-1">
                      {repo.metadata_status === 'ready' && (
                        <>
                          <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => {
                            if (editingMetadata[repo.repo_full_name] !== undefined) {
                              setEditingMetadata(prev => { const n = { ...prev }; delete n[repo.repo_full_name]; return n; });
                            } else {
                              setEditingMetadata(prev => ({ ...prev, [repo.repo_full_name]: repo.metadata_summary || '' }));
                            }
                          }} title="Edit description">
                            {editingMetadata[repo.repo_full_name] !== undefined ? <X className="h-3 w-3" /> : <Pencil className="h-3 w-3" />}
                          </Button>
                          <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => handleRegenerate(repo.repo_full_name)} title="Regenerate">
                            <RotateCw className="h-3 w-3" />
                          </Button>
                        </>
                      )}
                      {repo.metadata_status === 'error' && (
                        <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => handleRegenerate(repo.repo_full_name)}>Retry</Button>
                      )}
                    </div>
                  </div>

                  {/* Metadata display/edit */}
                  {(repo.metadata_status === 'pending' || repo.metadata_status === 'generating') && (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin" />Generating description...
                    </div>
                  )}
                  {repo.metadata_status === 'error' && (
                    <p className="text-xs text-red-500">Failed to generate description</p>
                  )}
                  {repo.metadata_status === 'ready' && editingMetadata[repo.repo_full_name] !== undefined && (
                    <div className="space-y-1">
                      <Textarea
                        value={editingMetadata[repo.repo_full_name]}
                        onChange={(e) => setEditingMetadata(prev => ({ ...prev, [repo.repo_full_name]: e.target.value }))}
                        className="text-xs min-h-[60px]"
                        rows={2}
                      />
                      <div className="flex gap-1 justify-end">
                        <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => setEditingMetadata(prev => { const n = { ...prev }; delete n[repo.repo_full_name]; return n; })}>Cancel</Button>
                        <Button size="sm" className="h-6 px-2 text-xs" onClick={() => handleMetadataSave(repo.repo_full_name)}>Save</Button>
                      </div>
                    </div>
                  )}
                  {repo.metadata_status === 'ready' && editingMetadata[repo.repo_full_name] === undefined && repo.metadata_summary && (
                    <p className="text-xs text-muted-foreground">{repo.metadata_summary?.replace(/\*\*/g, '')}</p>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Repo picker */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <p className="text-sm font-medium text-muted-foreground">
                  {savedRepos.length > 0 ? 'Edit Selection' : 'Select Repositories'}
                </p>
                {allRepos.length > 0 && <Badge variant="outline" className="text-xs">{allRepos.length} available</Badge>}
              </div>
            </div>

            {allRepos.length > 10 && (
              <div className="relative">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground" />
                <Input
                  placeholder="Filter repositories..."
                  value={searchFilter}
                  onChange={(e) => setSearchFilter(e.target.value)}
                  className="h-8 text-xs pl-7"
                />
              </div>
            )}

            <div className="max-h-60 overflow-y-auto space-y-1">
              {isLoadingRepos ? (
                <p className="text-xs text-muted-foreground">Loading repositories...</p>
              ) : filteredRepos.length > 0 ? (
                filteredRepos.map(repo => (
                  <label key={repo.id} className={`flex items-center gap-2 p-2 rounded-md border cursor-pointer hover:bg-muted/30 transition-colors ${
                    checkedRepos.has(repo.full_name) ? 'border-primary/50 bg-primary/5' : 'border-border'
                  }`}>
                    <Checkbox
                      checked={checkedRepos.has(repo.full_name)}
                      onCheckedChange={(checked) => {
                        setCheckedRepos(prev => {
                          const next = new Set(prev);
                          if (checked) next.add(repo.full_name); else next.delete(repo.full_name);
                          return next;
                        });
                      }}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium truncate">{repo.name}</span>
                        {repo.private && <Badge variant="secondary" className="text-xs">Private</Badge>}
                      </div>
                      {repo.name !== repo.full_name && (
                        <span className="text-xs text-muted-foreground truncate block">{repo.full_name}</span>
                      )}
                    </div>
                  </label>
                ))
              ) : (
                <p className="text-xs text-muted-foreground text-center py-4">
                  {searchFilter ? 'No repositories match your filter.' : 'No repositories available.'}
                </p>
              )}
            </div>

            {allRepos.length > 0 && (
              <Button
                onClick={handleSaveSelections}
                disabled={isSaving || checkedRepos.size === 0 || !hasUnsavedChanges}
                size="sm"
                className="w-full"
              >
                {isSaving ? <Loader2 className="h-3 w-3 animate-spin mr-2" /> : null}
                {hasUnsavedChanges ? `Save ${checkedRepos.size} Repositories` : `${checkedRepos.size} Repositories Saved`}
              </Button>
            )}
          </div>
        </div>
      )}
    </>
  );
}

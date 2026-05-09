'use client';

import { useState, useEffect, useCallback } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, ExternalLink, LogOut, ChevronDown, ChevronRight, RefreshCw, Search, Trash2, AlertCircle, ShieldAlert, FolderX } from 'lucide-react';
import Image from 'next/image';
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { useGitHubStatus, computeInstallationState, type InstallationState } from '@/hooks/use-github-status';
import { GitHubAppService, type GitHubInstallation } from '@/lib/github-app';
export type { GitHubInstallation, GitHubInstallationsResponse } from '@/lib/github-app';

export type GitHubAuthMethod = 'oauth' | 'app';

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
  // Optional metadata returned by /github/user-repos (Task 15) — used to filter
  // by GitHub App installation in the multi-installation picker.
  auth_method?: 'app' | 'oauth';
  installation_id?: number | null;
}

export interface ConnectedRepo {
  repo_full_name: string;
  repo_id: number;
  default_branch: string;
  is_private: boolean;
  repo_data: Repository | null;
  created_at: string | null;
}

export interface GitHubAuthConfig {
  mode: 'app' | 'oauth' | 'hybrid';
  app_enabled: boolean;
  oauth_enabled: boolean;
  oauth_configured: boolean;
}

export class GitHubIntegrationService {
  static async checkStatus(): Promise<GitHubCredentials> {
    const response = await fetch('/api/proxy/github/status');
    if (!response.ok) return { connected: false };
    return response.json();
  }

  static async getAuthConfig(): Promise<GitHubAuthConfig> {
    const response = await fetch('/api/proxy/github/auth-config');
    if (!response.ok) {
      // Default to App-only on error so a misconfigured proxy never
      // surfaces an OAuth CTA the deployment hasn't enabled.
      return { mode: 'app', app_enabled: true, oauth_enabled: false, oauth_configured: false };
    }
    return response.json();
  }

  static async initiateOAuth(): Promise<string> {
    const response = await fetch('/api/proxy/github/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (!response.ok) {
      const errorText = await response.text();
      let parsed: { error_code?: string; message?: string } | null = null;
      try {
        parsed = JSON.parse(errorText);
      } catch {
        // fall through
      }
      if (parsed?.error_code === 'GITHUB_NOT_CONFIGURED' || parsed?.error_code === 'GITHUB_OAUTH_DISABLED') {
        const err = new Error(parsed.message || 'GitHub OAuth is not configured');
        (err as Error & { errorCode?: string; isHandled?: boolean }).errorCode = parsed.error_code;
        (err as Error & { isHandled?: boolean }).isHandled = true;
        throw err;
      }
      throw new Error('Failed to initiate GitHub OAuth');
    }
    const data = await response.json();
    return data.oauth_url;
  }

  static async disconnect(): Promise<void> {
    await fetch('/api/proxy/github/disconnect', { method: 'POST' });
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  static async fetchRepositories(): Promise<any> {
    const response = await fetch('/api/proxy/github/user-repos');
    if (!response.ok) throw new Error('Failed to fetch repositories');
    return response.json();
  }

  static async fetchRepoSelections(): Promise<ConnectedRepo[]> {
    const response = await fetch('/api/proxy/github/repo-selections');
    if (!response.ok) return [];
    const data = await response.json();
    return data.repositories || [];
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  static async saveRepoSelections(repositories: Repository[]): Promise<any> {
    const response = await fetch('/api/proxy/github/repo-selections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repositories }),
    });
    if (!response.ok) throw new Error('Failed to save repository selections');
    return response.json();
  }

  static async clearRepoSelections(): Promise<void> {
    await fetch('/api/proxy/github/repo-selections', { method: 'DELETE' });
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
  const [hasLoadedRepos, setHasLoadedRepos] = useState(false);
  const [checkedRepos, setCheckedRepos] = useState<Set<string>>(new Set());
  const [searchFilter, setSearchFilter] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  // Saved repos
  const [savedRepos, setSavedRepos] = useState<ConnectedRepo[]>([]);

  // GitHub App installations linked to this user
  const [installations, setInstallations] = useState<GitHubInstallation[]>([]);
  const [isLoadingInstallations, setIsLoadingInstallations] = useState(false);
  const [installationFilter, setInstallationFilter] = useState<string>('all');

  // Server-fed deployment config: which auth paths are enabled.
  const [authConfig, setAuthConfig] = useState<GitHubAuthConfig>({
    mode: 'app',
    app_enabled: true,
    oauth_enabled: false,
    oauth_configured: false,
  });

  useEffect(() => {
    fetch('/api/getUserId').then(r => r.ok ? r.json() : null).then(d => {
      if (d?.userId) setUserId(d.userId);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!userId) return;
    GitHubIntegrationService.getAuthConfig()
      .then(setAuthConfig)
      .catch(() => { /* keep app-only default */ });
  }, [userId]);

  const fetchAllRepos = useCallback(async () => {
    if (!userId) return;
    setIsLoadingRepos(true);
    try {
      const data = await GitHubIntegrationService.fetchRepositories();
      const repos: Repository[] = Array.isArray(data) ? data : data?.repos || [];
      setAllRepos(repos);
    } catch { setAllRepos([]); }
    finally { setIsLoadingRepos(false); setHasLoadedRepos(true); }
  }, [userId]);

  const fetchInstallations = useCallback(async () => {
    if (!userId) return;
    setIsLoadingInstallations(true);
    try {
      const data = await GitHubAppService.listInstallations();
      setInstallations(data.installations || []);
    } catch { setInstallations([]); }
    finally { setIsLoadingInstallations(false); }
  }, [userId]);

  const loadSavedRepos = useCallback(async () => {
    if (!userId) return;
    try {
      const repos = await GitHubIntegrationService.fetchRepoSelections();
      setSavedRepos(repos);
      setCheckedRepos(new Set(repos.map(r => r.repo_full_name)));
    } catch { setSavedRepos([]); }
  }, [userId]);

  // Load saved repos when authenticated (fast DB query only)
  useEffect(() => {
    if (!githubStatus.isAuthenticated || !userId) return;
    loadSavedRepos();
  }, [githubStatus.isAuthenticated, userId, loadSavedRepos]);

  // Fetch GitHub App installations on mount; refresh when other parts of the
  // page emit `providerStateChanged` (e.g. after the install popup closes).
  useEffect(() => {
    if (!userId) return;
    fetchInstallations();
    const handler = () => fetchInstallations();
    window.addEventListener('providerStateChanged', handler);
    return () => window.removeEventListener('providerStateChanged', handler);
  }, [userId, fetchInstallations]);

  // Lazy-load full repo list only when user expands the picker
  useEffect(() => {
    if (expanded && githubStatus.isAuthenticated && userId && !hasLoadedRepos && !isLoadingRepos) {
      fetchAllRepos();
    }
  }, [expanded, githubStatus.isAuthenticated, userId, hasLoadedRepos, isLoadingRepos, fetchAllRepos]);

  const handleSaveSelections = async () => {
    if (!userId) return;
    setIsSaving(true);
    try {
      const selected = allRepos.filter(r => checkedRepos.has(r.full_name));
      await GitHubIntegrationService.saveRepoSelections(selected);
      toast({ title: "Repositories saved", description: `${selected.length} repositories connected.` });
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      await loadSavedRepos();
    } catch {
      toast({ title: "Error", description: "Failed to save repositories", variant: "destructive" });
    } finally { setIsSaving(false); }
  };

  const handleAppInstall = async () => {
    if (!userId) return;
    setIsLoading(true);
    try {
      const installUrl = await GitHubAppService.getInstallUrl();
      const installUrlState = new URL(installUrl).searchParams.get('state');

      if (!installUrlState) {
        throw new Error('GitHub App install URL is missing the required state parameter');
      }

      const popup = window.open(installUrl, 'github-app-install', 'width=600,height=700,scrollbars=yes,resizable=yes');
      if (!popup) {
        toast({
          title: "Popup Blocked",
          description: "Allow popups for Aurora to continue the GitHub App install flow.",
          variant: "destructive",
        });
        setIsLoading(false);
        return;
      }

      const checkClosed = setInterval(() => {
        if (popup.closed) {
          clearInterval(checkClosed);
          setIsLoading(false);
          setTimeout(() => {
            githubStatus.refresh();
            window.dispatchEvent(new CustomEvent('providerStateChanged'));
          }, 1000);
        }
      }, 1000);
    } catch (error: unknown) {
      const err = error as Error;
      toast({
        title: "GitHub App Install Unavailable",
        description: err.message || "Failed to prepare the GitHub App install flow.",
        variant: "destructive",
      });
      setIsLoading(false);
    }
  };

  const handleOAuthLogin = async () => {
    if (!userId) return;
    setIsLoading(true);
    try {
      const oauthUrl = await GitHubIntegrationService.initiateOAuth();
      const popup = window.open(oauthUrl, 'github-oauth', 'width=600,height=700,scrollbars=yes,resizable=yes');
      if (!popup) {
        toast({
          title: "Popup Blocked",
          description: "Allow popups for Aurora to continue the GitHub OAuth flow.",
          variant: "destructive",
        });
        setIsLoading(false);
        return;
      }
      const checkClosed = setInterval(() => {
        if (popup.closed) {
          clearInterval(checkClosed);
          setIsLoading(false);
          setTimeout(() => {
            githubStatus.refresh();
            window.dispatchEvent(new CustomEvent('providerStateChanged'));
          }, 1000);
        }
      }, 1000);
    } catch (error: unknown) {
      const err = error as Error & { errorCode?: string };
      const title = err.errorCode === 'GITHUB_NOT_CONFIGURED'
        ? 'GitHub OAuth Not Configured'
        : err.errorCode === 'GITHUB_OAUTH_DISABLED'
          ? 'GitHub OAuth Disabled'
          : 'Connection Failed';
      toast({
        title,
        description: err.message || 'Failed to connect to GitHub via OAuth',
        variant: 'destructive',
      });
      setIsLoading(false);
    }
  };

  const handleDisconnect = async () => {
    if (!userId) return;
    try {
      await GitHubIntegrationService.clearRepoSelections();
      await GitHubIntegrationService.disconnect();
      setSavedRepos([]);
      setCheckedRepos(new Set());
      setAllRepos([]);
      setHasLoadedRepos(false);
      setExpanded(false);
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Disconnected", description: "GitHub account disconnected" });
    } catch (error: unknown) {
      const err = error as Error;
      toast({
        title: "Disconnect failed",
        description: err.message || "Failed to disconnect GitHub. The connection may still be active on the server.",
        variant: "destructive",
      });
    }
  };

  const handleUnlinkInstallation = async (installationId: number) => {
    if (!userId) return;
    try {
      await GitHubAppService.unlinkInstallation(installationId);
      toast({ title: "Installation unlinked", description: "GitHub App installation removed from Aurora" });
      await fetchInstallations();
      if (installationFilter === String(installationId)) setInstallationFilter('all');
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
    } catch {
      toast({ title: "Error", description: "Failed to unlink installation", variant: "destructive" });
    }
  };

  if (!userId || githubStatus.hasReposConnected === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground p-3 border border-border rounded-lg">
        <Loader2 className="w-4 h-4 animate-spin" />Checking GitHub connection...
      </div>
    );
  }

  const searchedRepos = allRepos.filter(r =>
    r.full_name.toLowerCase().includes(searchFilter.toLowerCase())
  );

  const visibleRepos = installationFilter === 'all'
    ? searchedRepos
    : searchedRepos.filter(r => r.installation_id != null && String(r.installation_id) === installationFilter);

  const installationRepoSummary = (installation: GitHubInstallation): string => {
    if (installation.repository_selection === 'all') return 'All repositories';
    if (!hasLoadedRepos) return 'Selected repositories';
    const count = allRepos.filter(r => r.installation_id === installation.installation_id).length;
    return `${count} selected repositor${count === 1 ? 'y' : 'ies'}`;
  };

  const installationManageUrl = (installation: GitHubInstallation): string =>
    installation.account_type === 'Organization'
      ? `https://github.com/organizations/${installation.account_login}/settings/installations/${installation.installation_id}`
      : `https://github.com/settings/installations/${installation.installation_id}`;

  const activeInstallation: GitHubInstallation | null = (() => {
    if (installations.length === 0) return null;
    if (installations.length === 1) return installations[0];
    if (installationFilter === 'all') return null;
    return installations.find(inst => String(inst.installation_id) === installationFilter) ?? null;
  })();

  const activeInstallationState: InstallationState = activeInstallation
    ? computeInstallationState(activeInstallation, allRepos, { reposLoaded: hasLoadedRepos })
    : 'ok';

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
          {githubStatus.isAuthenticated && (
            <div className="flex gap-2">
              <Button variant="ghost" size="sm" className="px-3" onClick={(e) => { e.stopPropagation(); fetchAllRepos(); loadSavedRepos(); }} title="Refresh">
                <RefreshCw className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="sm" className="px-3 text-red-600 hover:text-red-700 hover:bg-red-50" onClick={(e) => { e.stopPropagation(); handleDisconnect(); }} title="Disconnect">
                <LogOut className="h-4 w-4" />
              </Button>
            </div>
          )}
          {githubStatus.isAuthenticated && (
            <button
              type="button"
              className="p-1 hover:bg-muted rounded"
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
              aria-label={expanded ? 'Collapse GitHub section' : 'Expand GitHub section'}
              aria-expanded={expanded}
            >
              {expanded ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
            </button>
          )}
        </div>
      </div>

      {/* Connect CTAs — rendered based on the deployment's auth mode.
          App-only deployments see one button; OAuth-only see one button;
          hybrid deployments see both with App as the recommended path. */}
      {!githubStatus.isAuthenticated && (
        <div className="mt-2 p-4 border border-border rounded-lg bg-muted/30 space-y-3">
          <p className="text-sm text-muted-foreground leading-relaxed">
            {authConfig.app_enabled && authConfig.oauth_enabled ? (
              <>
                Choose how to connect.{' '}
                <span className="font-medium text-foreground">Install the GitHub App</span> for
                higher rate limits, fine-grained permissions, and real-time webhooks (recommended).{' '}
                <span className="font-medium text-foreground">Connect via OAuth</span> uses your
                personal access token — simpler, no admin needed.
              </>
            ) : authConfig.oauth_enabled ? (
              <>
                <span className="font-medium text-foreground">Connect via OAuth</span> uses your
                personal GitHub access token. The administrator of this Aurora deployment has
                disabled the GitHub App path.
              </>
            ) : (
              <>
                <span className="font-medium text-foreground">Install the GitHub App</span> on your
                organization to give Aurora read access to repos, deployments, and workflow runs.
                Higher API rate limits, fine-grained repository permissions, and real-time webhook
                delivery are all included.
              </>
            )}
          </p>
          <div className="flex flex-wrap gap-2">
            {authConfig.app_enabled && (
              <Button
                onClick={handleAppInstall}
                disabled={isLoading || !userId}
                size="sm"
                data-testid="github-install-app-cta"
              >
                {isLoading ? <Loader2 className="h-3 w-3 animate-spin mr-2" /> : null}
                Install GitHub App
              </Button>
            )}
            {authConfig.oauth_enabled && (
              <Button
                onClick={handleOAuthLogin}
                disabled={isLoading || !userId}
                size="sm"
                variant={authConfig.app_enabled ? 'outline' : 'default'}
                data-testid="github-oauth-cta"
              >
                {isLoading ? <Loader2 className="h-3 w-3 animate-spin mr-2" /> : null}
                Connect via OAuth
              </Button>
            )}
          </div>
        </div>
      )}

      {/* Expanded content */}
      {expanded && githubStatus.isAuthenticated && (
        <div className="ml-6 border-l-2 border-muted pl-6 mt-2 space-y-3">
          {/* GitHub App installations (rendered only when at least one exists or first load is in flight) */}
          {(installations.length > 0 || (isLoadingInstallations && installations.length === 0)) && (
            <div className="space-y-2" data-testid="installations-section">
              <p className="text-sm font-medium text-muted-foreground">Connected GitHub Installations</p>
              {isLoadingInstallations && installations.length === 0 ? (
                <div className="space-y-2" data-testid="installations-skeleton">
                  {[0, 1].map(i => (
                    <div key={i} className="p-3 rounded-md border border-border animate-pulse">
                      <div className="flex items-center gap-3">
                        <div className="h-10 w-10 rounded-full bg-muted flex-shrink-0" />
                        <div className="flex-1 space-y-2">
                          <div className="h-3 w-32 bg-muted rounded" />
                          <div className="h-3 w-48 bg-muted rounded" />
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                installations.map(installation => (
                  <div
                    key={installation.installation_id}
                    className="p-3 rounded-md border border-border"
                    data-testid={`installation-card-${installation.installation_id}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-start gap-3 min-w-0 flex-1">
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={`https://github.com/${installation.account_login}.png?size=40`}
                          alt={`${installation.account_login} avatar`}
                          width={40}
                          height={40}
                          loading="lazy"
                          className="rounded-full flex-shrink-0 bg-muted"
                        />
                        <div className="min-w-0 flex-1 space-y-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-sm font-medium truncate">{installation.account_login}</span>
                            <Badge variant="secondary" className="text-xs">{installation.account_type}</Badge>
                            {installation.suspended_at && (
                              <Badge variant="destructive" className="text-xs" data-testid={`installation-suspended-${installation.installation_id}`}>Suspended</Badge>
                            )}
                            {installation.permissions_pending_update && (
                              <Badge variant="outline" className="text-xs border-yellow-500 text-yellow-600" data-testid={`installation-pending-${installation.installation_id}`}>Pending Permissions</Badge>
                            )}
                          </div>
                          <p className="text-xs text-muted-foreground">{installationRepoSummary(installation)}</p>
                        </div>
                      </div>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs"
                          onClick={() => window.open(installationManageUrl(installation), '_blank', 'noopener,noreferrer')}
                          title="Manage on GitHub"
                          data-testid={`installation-manage-${installation.installation_id}`}
                        >
                          <ExternalLink className="h-3 w-3 mr-1" />
                          Manage
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 w-7 p-0 text-red-600 hover:text-red-700 hover:bg-red-50"
                          onClick={() => handleUnlinkInstallation(installation.installation_id)}
                          title="Unlink installation"
                          data-testid={`installation-unlink-${installation.installation_id}`}
                        >
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          )}

          {/* Connected repos */}
          {savedRepos.length > 0 && (
            <div className="space-y-2">
              <p className="text-sm font-medium text-muted-foreground">Connected Repositories</p>
              {savedRepos.map(repo => (
                <div key={repo.repo_full_name} className="p-2 rounded-md border border-border">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{repo.repo_full_name}</span>
                    {repo.is_private && <Badge variant="secondary" className="text-xs">Private</Badge>}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Repo picker (replaced with state banner when the active installation is non-OK) */}
          {activeInstallation && activeInstallationState !== 'ok' ? (
            <Alert
              variant={activeInstallationState === 'suspended' ? 'destructive' : 'default'}
              data-testid={`installation-banner-${activeInstallationState}`}
            >
              {activeInstallationState === 'suspended' && <ShieldAlert className="h-4 w-4" />}
              {activeInstallationState === 'pending_permissions' && <AlertCircle className="h-4 w-4" />}
              {activeInstallationState === 'no_repos' && <FolderX className="h-4 w-4" />}
              <AlertTitle>
                {activeInstallationState === 'suspended' && 'Installation suspended'}
                {activeInstallationState === 'pending_permissions' && 'New permissions required'}
                {activeInstallationState === 'no_repos' && 'No repositories accessible'}
              </AlertTitle>
              <AlertDescription className="space-y-3">
                <p>
                  {activeInstallationState === 'suspended' && (
                    <>This installation is suspended. Re-enable it on GitHub to continue using Aurora with these repos.</>
                  )}
                  {activeInstallationState === 'pending_permissions' && (
                    <>Aurora needs new permissions. Click below to review and accept on GitHub.</>
                  )}
                  {activeInstallationState === 'no_repos' && (
                    <>No repositories are accessible to this installation. Add repositories on GitHub.</>
                  )}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => window.open(installationManageUrl(activeInstallation), '_blank', 'noopener,noreferrer')}
                  data-testid={`installation-banner-cta-${activeInstallationState}`}
                >
                  <ExternalLink className="h-3 w-3 mr-1" />
                  {activeInstallationState === 'suspended' && 'Re-enable on GitHub'}
                  {activeInstallationState === 'pending_permissions' && 'Review permissions on GitHub'}
                  {activeInstallationState === 'no_repos' && 'Add repositories on GitHub'}
                </Button>
              </AlertDescription>
            </Alert>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-muted-foreground">
                    {savedRepos.length > 0 ? 'Edit Selection' : 'Select Repositories'}
                  </p>
                  {allRepos.length > 0 && <Badge variant="outline" className="text-xs">{allRepos.length} available</Badge>}
                </div>
              </div>

              {installations.length > 1 && (
                <div className="space-y-1" data-testid="installation-filter">
                  <p className="text-xs text-muted-foreground">Filter by installation</p>
                  <Select value={installationFilter} onValueChange={setInstallationFilter}>
                    <SelectTrigger className="h-8 text-xs">
                      <SelectValue placeholder="All installations" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">All installations</SelectItem>
                      {installations.map(installation => (
                        <SelectItem key={installation.installation_id} value={String(installation.installation_id)}>
                          {installation.account_login} ({installation.account_type})
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}

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
                ) : visibleRepos.length > 0 ? (
                  visibleRepos.map(repo => (
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
                    {searchFilter || installationFilter !== 'all' ? 'No repositories match your filter.' : 'No repositories available.'}
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
          )}
        </div>
      )}
    </>
  );
}

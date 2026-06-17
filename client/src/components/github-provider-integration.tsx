'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { ToastAction } from '@/components/ui/toast';
import { Loader2, Check, ExternalLink, LogOut, ChevronDown, ChevronRight, RefreshCw, Pencil, X, Search, RotateCw, Trash2, AlertCircle, ShieldAlert, FolderX } from 'lucide-react';
import Image from 'next/image';
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useGitHubStatus, computeInstallationState, type InstallationState } from '@/hooks/use-github-status';
import { GitHubAppService, type GitHubInstallation, type GitHubDiscoveredInstallation } from '@/lib/github-app';
import { queryClient } from '@/lib/query';

const setGithubConnectedOptimistically = (connected: boolean) => {
  const key = '/api/connectors/status';
  const prev = queryClient.read<Record<string, boolean>>(key) ?? {};
  if (prev.github === connected) return;
  queryClient.set(key, { ...prev, github: connected });
};
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
  metadata_summary: string | null;
  metadata_status: string;
  repo_data: Repository | null;
  created_at: string | null;
  // PR change gating (incident prevention) — only settable on App-linked repos.
  change_gating_enabled?: boolean;
  installation_id?: number | null;
}

export interface GitHubAuthConfig {
  mode: 'app' | 'oauth' | 'hybrid';
  app_enabled: boolean;
  oauth_enabled: boolean;
  oauth_configured: boolean;
  incident_prevention_enabled: boolean;
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
      return { mode: 'app', app_enabled: true, oauth_enabled: false, oauth_configured: false, incident_prevention_enabled: true };
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

  static async disconnect(opts: { alsoUninstall?: boolean } = {}): Promise<{
    uninstalled_on_github: number;
    uninstall_failures: number;
  }> {
    const response = await fetch('/api/proxy/github/disconnect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ also_uninstall: !!opts.alsoUninstall }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(data?.error || 'Failed to disconnect GitHub');
    }
    return {
      uninstalled_on_github: data?.uninstalled_on_github ?? 0,
      uninstall_failures: data?.uninstall_failures ?? 0,
    };
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
    const response = await fetch('/api/proxy/github/repo-selections', { method: 'DELETE' });
    if (!response.ok) {
      throw new Error('Failed to clear repository selections');
    }
  }

  static async updateRepoMetadata(repoFullName: string, summary: string): Promise<void> {
    const response = await fetch(`/api/proxy/github/repo-selections/${encodeURIComponent(repoFullName)}/metadata`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ metadata_summary: summary }),
    });
    if (!response.ok) throw new Error('Failed to update metadata');
  }

  static async setChangeGating(repoFullName: string, enabled: boolean): Promise<void> {
    const response = await fetch(`/api/proxy/github/repo-selections/${encodeURIComponent(repoFullName)}/change-gating`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(data?.error || 'Failed to update incident prevention setting');
    }
  }

  static async generateRepoMetadata(repoFullName: string): Promise<void> {
    const response = await fetch('/api/proxy/github/repo-metadata/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_full_name: repoFullName }),
    });
    if (!response.ok) throw new Error('Failed to trigger metadata generation');
  }
}

function getAuthCallbackOrigins(): Set<string> {
  const origins = new Set<string>();
  origins.add(window.location.origin);
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || '';
  if (backendUrl) {
    try {
      origins.add(new URL(backendUrl).origin);
    } catch {
      /* ignore malformed env */
    }
  }
  return origins;
}

export default function GitHubProviderIntegration() {
  const githubStatus = useGitHubStatus();
  const [isInstallingApp, setIsInstallingApp] = useState(false);
  const [isConnectingOAuth, setIsConnectingOAuth] = useState(false);
  const { toast } = useToast();

  // Repo picker state
  const [allRepos, setAllRepos] = useState<Repository[]>([]);
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);
  const [hasLoadedRepos, setHasLoadedRepos] = useState(false);
  const [checkedRepos, setCheckedRepos] = useState<Set<string>>(new Set());
  const [searchFilter, setSearchFilter] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const [savedRepos, setSavedRepos] = useState<ConnectedRepo[]>([]);
  const [savedReposLoaded, setSavedReposLoaded] = useState(false);
  const [editingMetadata, setEditingMetadata] = useState<Record<string, string>>({});
  const [gatingUpdating, setGatingUpdating] = useState<Set<string>>(new Set());
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const popupCleanupsRef = useRef<Array<() => void>>([]);

  const wasAuthenticatedRef = useRef<boolean | null>(null);
  const expectedDisconnectRef = useRef(false);
  const lastAccountLoginsRef = useRef<string[]>([]);

  const [showDisconnectDialog, setShowDisconnectDialog] = useState(false);
  const [alsoUninstallOnGitHub, setAlsoUninstallOnGitHub] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);

  // GitHub App installations linked to this user
  const [installations, setInstallations] = useState<GitHubInstallation[]>([]);
  const [installationFilter, setInstallationFilter] = useState<string>('all');

  // App installations that exist on GitHub but aren't linked to this Aurora
  // user. Surfaced after Install GitHub App when GitHub didn't fire the
  // install callback (because the App was already installed) so the user
  // can explicitly claim them.
  const [discoveredInstallations, setDiscoveredInstallations] = useState<GitHubDiscoveredInstallation[]>([]);
  const [isClaimingInstallation, setIsClaimingInstallation] = useState(false);

  // Server-fed deployment config: which auth paths are enabled.
  const [authConfig, setAuthConfig] = useState<GitHubAuthConfig>({
    mode: 'app',
    app_enabled: true,
    oauth_enabled: false,
    oauth_configured: false,
    incident_prevention_enabled: true,
  });

  useEffect(() => {
    GitHubIntegrationService.getAuthConfig()
      .then(setAuthConfig)
      .catch(() => { /* keep app-only default */ });
  }, []);

  const fetchAllRepos = useCallback(async () => {
    setIsLoadingRepos(true);
    try {
      const data = await GitHubIntegrationService.fetchRepositories();
      const repos: Repository[] = Array.isArray(data) ? data : data?.repos || [];
      setAllRepos(repos);
    } catch { setAllRepos([]); }
    finally { setIsLoadingRepos(false); setHasLoadedRepos(true); }
  }, []);

  const fetchInstallations = useCallback(async () => {
    try {
      const data = await GitHubAppService.listInstallations();
      const linked = data.installations || [];
      setInstallations(linked);
      if (linked.length === 0 && authConfig.app_enabled) {
        try {
          const discovered = await GitHubAppService.discoverInstallations();
          setDiscoveredInstallations(discovered.installations || []);
        } catch {
          setDiscoveredInstallations([]);
        }
      } else {
        setDiscoveredInstallations([]);
      }
    } catch { setInstallations([]); }
  }, [authConfig.app_enabled]);

  const startMetadataPolling = useCallback((repos: ConnectedRepo[]) => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    // Polling disabled — 3s interval causes frontend OOM in dev mode (see #471).
    // Metadata status updates on manual page refresh instead.
    return;
  }, []);

  const loadSavedRepos = useCallback(async () => {
    try {
      const repos = await GitHubIntegrationService.fetchRepoSelections();
      setSavedRepos(repos);
      setCheckedRepos(new Set(repos.map(r => r.repo_full_name)));
      startMetadataPolling(repos);
    } catch { setSavedRepos([]); }
    finally { setSavedReposLoaded(true); }
  }, [startMetadataPolling]);

  useEffect(() => {
    if (installations.length > 0) {
      lastAccountLoginsRef.current = installations.map(i => i.account_login);
    }
  }, [installations]);

  useEffect(() => {
    if (githubStatus.hasReposConnected === null) return;

    const prev = wasAuthenticatedRef.current;
    wasAuthenticatedRef.current = githubStatus.isAuthenticated;

    if (prev !== true || githubStatus.isAuthenticated !== false) return;

    if (expectedDisconnectRef.current) {
      expectedDisconnectRef.current = false;
      return;
    }

    const names = lastAccountLoginsRef.current;
    const accountPhrase =
      names.length === 0 ? '' :
      names.length === 1 ? ` from ${names[0]}` :
      ` from ${names.length} accounts (${names.slice(0, 2).join(', ')}${names.length > 2 ? ', …' : ''})`;
    toast({
      title: 'GitHub App was uninstalled',
      description: `Aurora detected the App was removed${accountPhrase} on GitHub. Reinstall it any time from this connector to re-enable investigations.`,
      variant: 'destructive',
    });
    lastAccountLoginsRef.current = [];
  }, [githubStatus.isAuthenticated, githubStatus.hasReposConnected, toast]);

  useEffect(() => () => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    for (const cleanup of popupCleanupsRef.current) {
      try { cleanup(); } catch { /* swallow — cleanup must not throw */ }
    }
    popupCleanupsRef.current = [];
  }, []);

  // Load saved repos when authenticated (fast DB query only)
  useEffect(() => {
    if (!githubStatus.isAuthenticated) return;
    loadSavedRepos();
  }, [githubStatus.isAuthenticated, loadSavedRepos]);

  useEffect(() => {
    fetchInstallations();
    const handler = () => fetchInstallations();
    const onMessage = (event: MessageEvent) => {
      if (!getAuthCallbackOrigins().has(event.origin)) return;
      const data = event.data as { type?: string } | null;
      if (data && data.type === 'github_auth_success') fetchInstallations();
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') fetchInstallations();
    };
    window.addEventListener('providerStateChanged', handler);
    window.addEventListener('message', onMessage);
    window.addEventListener('focus', handler);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('providerStateChanged', handler);
      window.removeEventListener('message', onMessage);
      window.removeEventListener('focus', handler);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [fetchInstallations]);

  // Lazy-load full repo list only when user expands the picker
  useEffect(() => {
    if (expanded && githubStatus.isAuthenticated && !hasLoadedRepos && !isLoadingRepos) {
      fetchAllRepos();
    }
  }, [expanded, githubStatus.isAuthenticated, hasLoadedRepos, isLoadingRepos, fetchAllRepos]);

  const handleSaveSelections = async () => {
    setIsSaving(true);
    try {
      const selected = allRepos.filter(r => checkedRepos.has(r.full_name));
      if (selected.length === 0) {
        await GitHubIntegrationService.clearRepoSelections();
      } else {
        await GitHubIntegrationService.saveRepoSelections(selected);
      }
      toast({ title: "Repositories saved", description: `${selected.length} repositories connected.` });
      setGithubConnectedOptimistically(selected.length > 0);
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      await loadSavedRepos();
    } catch {
      toast({ title: "Error", description: "Failed to save repositories", variant: "destructive" });
    } finally { setIsSaving(false); }
  };

  const handleAppInstall = async () => {
    setIsInstallingApp(true);
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
        setIsInstallingApp(false);
        return;
      }

      let checkClosed: ReturnType<typeof setInterval> | null = null;
      let finalized = false;
      const cleanup = () => {
        if (checkClosed !== null) {
          clearInterval(checkClosed);
          checkClosed = null;
        }
        window.removeEventListener('message', onMessage);
        document.removeEventListener('visibilitychange', onVisibility);
      };
      const finalize = async () => {
        if (finalized) return;
        finalized = true;
        cleanup();
        popupCleanupsRef.current = popupCleanupsRef.current.filter(c => c !== cleanup);
        setIsInstallingApp(false);
        githubStatus.refresh();
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
        try {
          const linked = await GitHubAppService.listInstallations();
          const linkedCount = linked.installations?.length ?? 0;
          setGithubConnectedOptimistically(linkedCount > 0);
          if (linkedCount === 0) {
            const discovered = await GitHubAppService.discoverInstallations();
            setDiscoveredInstallations(discovered.installations || []);
          } else {
            setDiscoveredInstallations([]);
          }
        } catch {
          // discovery is best-effort; silent on failure
        }
      };
      const onVisibility = () => {
        if (document.visibilityState !== 'visible') return;
        if (popup.closed || finalized) {
          finalize();
        }
      };
      document.addEventListener('visibilitychange', onVisibility);

      const onMessage = (event: MessageEvent) => {
        if (event.source !== popup) return;
        if (!getAuthCallbackOrigins().has(event.origin)) return;
        const data = event.data as { type?: string } | null;
        if (data && data.type === 'github_auth_success') {
          finalize();
        }
      };
      window.addEventListener('message', onMessage);

      checkClosed = setInterval(() => {
        if (popup.closed) {
          finalize();
        }
      }, 500);
      popupCleanupsRef.current.push(cleanup);
    } catch (error: unknown) {
      const err = error as Error;
      toast({
        title: "GitHub App Install Unavailable",
        description: err.message || "Failed to prepare the GitHub App install flow.",
        variant: "destructive",
      });
      setIsInstallingApp(false);
    }
  };

  const handleMetadataSave = async (repoFullName: string) => {
    const summary = editingMetadata[repoFullName];
    if (summary === undefined) return;
    try {
      await GitHubIntegrationService.updateRepoMetadata(repoFullName, summary);
      setSavedRepos(prev => prev.map(r =>
        r.repo_full_name === repoFullName ? { ...r, metadata_summary: summary, metadata_status: 'ready' } : r
      ));
      setEditingMetadata(prev => {
        const next = { ...prev };
        delete next[repoFullName];
        return next;
      });
      toast({ title: "Description updated" });
    } catch {
      toast({ title: "Error", description: "Failed to update description", variant: "destructive" });
    }
  };

  const handleChangeGatingToggle = async (repoFullName: string, enabled: boolean) => {
    setGatingUpdating(prev => new Set(prev).add(repoFullName));
    setSavedRepos(prev => prev.map(r =>
      r.repo_full_name === repoFullName ? { ...r, change_gating_enabled: enabled } : r
    ));
    try {
      await GitHubIntegrationService.setChangeGating(repoFullName, enabled);
      // Re-assert the confirmed value: a loadSavedRepos poll snapshotted
      // before the PUT committed can land after the optimistic update and
      // clobber it with the stale flag.
      setSavedRepos(prev => prev.map(r =>
        r.repo_full_name === repoFullName ? { ...r, change_gating_enabled: enabled } : r
      ));
      globalThis.dispatchEvent(new CustomEvent('providerStateChanged'));
    } catch (error: unknown) {
      const err = error as Error;
      setSavedRepos(prev => prev.map(r =>
        r.repo_full_name === repoFullName ? { ...r, change_gating_enabled: !enabled } : r
      ));
      toast({
        title: "Error",
        description: err.message || "Failed to update incident prevention setting",
        variant: "destructive",
      });
    } finally {
      setGatingUpdating(prev => {
        const next = new Set(prev);
        next.delete(repoFullName);
        return next;
      });
    }
  };

  const handleRegenerate = async (repoFullName: string) => {
    try {
      await GitHubIntegrationService.generateRepoMetadata(repoFullName);
      const updated = savedRepos.map(r =>
        r.repo_full_name === repoFullName ? { ...r, metadata_status: 'generating' } : r
      );
      setSavedRepos(updated);
      startMetadataPolling(updated);
    } catch {
      toast({ title: "Error", description: "Failed to regenerate description", variant: "destructive" });
    }
  };

  const handleClaimInstallation = async (installationId: number) => {
    setIsClaimingInstallation(true);
    try {
      await GitHubAppService.claimInstallation(installationId);
      setDiscoveredInstallations(prev => prev.filter(d => d.installation_id !== installationId));
      setGithubConnectedOptimistically(true);
      await fetchInstallations();
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Linked", description: "GitHub App installation linked to Aurora" });
    } catch (error: unknown) {
      const err = error as Error;
      toast({
        title: "Link failed",
        description: err.message || "Failed to link installation",
        variant: "destructive",
      });
    } finally {
      setIsClaimingInstallation(false);
    }
  };

  const handleOAuthLogin = async () => {
    setIsConnectingOAuth(true);
    try {
      const oauthUrl = await GitHubIntegrationService.initiateOAuth();
      const popup = window.open(oauthUrl, 'github-oauth', 'width=600,height=700,scrollbars=yes,resizable=yes');
      if (!popup) {
        toast({
          title: "Popup Blocked",
          description: "Allow popups for Aurora to continue the GitHub OAuth flow.",
          variant: "destructive",
        });
        setIsConnectingOAuth(false);
        return;
      }

      let checkClosed: ReturnType<typeof setInterval> | null = null;
      let finalized = false;
      const cleanup = () => {
        if (checkClosed !== null) {
          clearInterval(checkClosed);
          checkClosed = null;
        }
        window.removeEventListener('message', onMessage);
        document.removeEventListener('visibilitychange', onVisibility);
      };
      const finalize = (markConnected = false) => {
        if (finalized) return;
        finalized = true;
        cleanup();
        popupCleanupsRef.current = popupCleanupsRef.current.filter(c => c !== cleanup);
        setIsConnectingOAuth(false);
        if (markConnected) {
          setGithubConnectedOptimistically(true);
        }
        githubStatus.refresh();
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
      };
      const onMessage = (event: MessageEvent) => {
        if (event.source !== popup) return;
        if (!getAuthCallbackOrigins().has(event.origin)) return;
        const data = event.data as { type?: string } | null;
        if (data && data.type === 'github_auth_success') {
          finalize(true);
        }
      };
      const onVisibility = () => {
        if (document.visibilityState !== 'visible') return;
        if (popup.closed || finalized) finalize();
      };
      window.addEventListener('message', onMessage);
      document.addEventListener('visibilitychange', onVisibility);
      checkClosed = setInterval(() => {
        if (popup.closed) finalize();
      }, 500);
      popupCleanupsRef.current.push(cleanup);
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
      setIsConnectingOAuth(false);
    }
  };

  const openDisconnectDialog = () => {
    setAlsoUninstallOnGitHub(false);
    setShowDisconnectDialog(true);
  };

  const handleDisconnect = async (alsoUninstall: boolean) => {
    const hadAppInstall = installations.length > 0;
    const primaryInstall = installations[0];
    setIsDisconnecting(true);
    try {
      await GitHubIntegrationService.clearRepoSelections();
      const result = await GitHubIntegrationService.disconnect({ alsoUninstall });
      expectedDisconnectRef.current = true;
      setSavedRepos([]);
      setSavedReposLoaded(false);
      setCheckedRepos(new Set());
      setAllRepos([]);
      setHasLoadedRepos(false);
      setExpanded(false);
      setInstallations([]);
      setGithubConnectedOptimistically(false);
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      setShowDisconnectDialog(false);

      if (alsoUninstall && hadAppInstall) {
        const uninstalled = result?.uninstalled_on_github ?? 0;
        const failures = result?.uninstall_failures ?? 0;
        if (failures > 0) {
          toast({
            title: "Disconnected, partial uninstall",
            description: `Uninstalled ${uninstalled} on GitHub; ${failures} could not be removed. Visit GitHub settings to finish.`,
            variant: "destructive",
          });
        } else {
          toast({
            title: "Disconnected and uninstalled",
            description: `Removed Aurora and uninstalled the GitHub App from ${uninstalled} account${uninstalled === 1 ? '' : 's'}.`,
          });
        }
      } else if (hadAppInstall && primaryInstall) {
        const manageUrl = installationManageUrl(primaryInstall);
        toast({
          title: "Disconnected from Aurora",
          description: "The GitHub App is still installed on your GitHub side. Open GitHub to fully uninstall it if you no longer want Aurora to receive webhooks.",
          action: (
            <ToastAction
              altText="Uninstall on GitHub"
              onClick={() => window.open(manageUrl, '_blank', 'noopener,noreferrer')}
            >
              Uninstall on GitHub
            </ToastAction>
          ),
        });
      } else {
        toast({ title: "Disconnected", description: "GitHub account disconnected" });
      }
    } catch (error: unknown) {
      const err = error as Error;
      toast({
        title: "Disconnect failed",
        description: err.message || "Failed to disconnect GitHub. The connection may still be active on the server.",
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
    }
  };

  const handleUnlinkInstallation = async (installationId: number) => {
    try {
      await GitHubAppService.unlinkInstallation(installationId);
      if (installations.length <= 1) {
        expectedDisconnectRef.current = true;
      }
      toast({ title: "Installation unlinked", description: "GitHub App installation removed from Aurora" });
      if (installations.length <= 1) {
        setGithubConnectedOptimistically(false);
      }
      await fetchInstallations();
      if (installationFilter === String(installationId)) setInstallationFilter('all');
      githubStatus.refresh();
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
    } catch {
      toast({ title: "Error", description: "Failed to unlink installation", variant: "destructive" });
    }
  };

  if (githubStatus.hasReposConnected === null) {
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
        role="button"
        tabIndex={githubStatus.isAuthenticated ? 0 : -1}
        aria-expanded={expanded}
        aria-disabled={!githubStatus.isAuthenticated}
        className="flex items-center justify-between p-3 border border-border rounded-lg hover:bg-muted/50 transition-colors cursor-pointer"
        onClick={() => { if (githubStatus.isAuthenticated) setExpanded(!expanded); }}
        onKeyDown={(e) => {
          if (!githubStatus.isAuthenticated) return;
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setExpanded(!expanded);
          }
        }}
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
                  {savedReposLoaded ? (
                    <span className="text-xs text-muted-foreground">{savedRepos.length} repo{savedRepos.length !== 1 ? 's' : ''} connected</span>
                  ) : (
                    <span className="flex items-center gap-1 text-xs text-muted-foreground">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      Loading repositories…
                    </span>
                  )}
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
              <Button variant="ghost" size="sm" className="px-3 text-red-600 hover:text-red-700 hover:bg-red-50" onClick={(e) => { e.stopPropagation(); openDisconnectDialog(); }} title="Disconnect">
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

      {/* Existing-install picker — surfaced when the user clicked Install
          GitHub App but GitHub didn't redirect with a fresh installation_id
          (because the App was already installed on their org). Lets them
          explicitly claim the existing installation. */}
      {!githubStatus.isAuthenticated && discoveredInstallations.length > 0 && (
        <div
          className="mt-2 p-4 border border-amber-500/40 rounded-lg bg-amber-500/10 space-y-3"
          data-testid="discovered-installations"
        >
          <div className="space-y-1">
            <p className="text-sm font-medium text-foreground">
              Existing installation{discoveredInstallations.length > 1 ? 's' : ''} found on GitHub
            </p>
            <p className="text-xs text-muted-foreground">
              The Aurora GitHub App is already installed
              {discoveredInstallations.length > 1 ? ' on multiple accounts' : ''}.
              Claim the one that belongs to you to finish connecting.
            </p>
          </div>
          <div className="space-y-2">
            {discoveredInstallations.map(d => (
              <div
                key={d.installation_id}
                className="flex items-center justify-between gap-3 p-2 rounded-md border border-border bg-background"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate">{d.account_login || `Installation ${d.installation_id}`}</p>
                  {d.account_type && (
                    <p className="text-xs text-muted-foreground">{d.account_type}</p>
                  )}
                </div>
                <Button
                  size="sm"
                  onClick={() => handleClaimInstallation(d.installation_id)}
                  disabled={isClaimingInstallation}
                  data-testid={`claim-installation-${d.installation_id}`}
                >
                  {isClaimingInstallation ? <Loader2 className="h-3 w-3 animate-spin mr-2" /> : null}
                  Link to Aurora
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}

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
                disabled={isInstallingApp || isConnectingOAuth}
                size="sm"
                data-testid="github-install-app-cta"
              >
                {isInstallingApp ? <Loader2 className="h-3 w-3 animate-spin mr-2" /> : null}
                Install GitHub App
              </Button>
            )}
            {authConfig.oauth_enabled && (
              <Button
                onClick={handleOAuthLogin}
                disabled={isInstallingApp || isConnectingOAuth}
                size="sm"
                variant={authConfig.app_enabled ? 'outline' : 'default'}
                data-testid="github-oauth-cta"
              >
                {isConnectingOAuth ? <Loader2 className="h-3 w-3 animate-spin mr-2" /> : null}
                Connect via OAuth
              </Button>
            )}
          </div>
        </div>
      )}

      {/* Expanded content */}
      {expanded && githubStatus.isAuthenticated && (
        <div className="ml-6 border-l-2 border-muted pl-6 mt-2 space-y-3">
          {installations.length > 0 && (
            <div className="space-y-2" data-testid="installations-section">
              <p className="text-sm font-medium text-muted-foreground">Connected GitHub Installations</p>
              {installations.map(installation => (
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
                ))}
            </div>
          )}

          {/* Connected repos with metadata */}
          {savedRepos.length > 0 && (
            <div className="space-y-2">
              <p className="text-sm font-medium text-muted-foreground">Connected Repositories</p>
              {savedRepos.map(repo => {
                const isEditing = editingMetadata[repo.repo_full_name] !== undefined;
                const isReady = repo.metadata_status === 'ready';
                const isPending = repo.metadata_status === 'pending' || repo.metadata_status === 'generating';
                const isError = repo.metadata_status === 'error' || repo.metadata_status === 'limit_reached';
                const isLimitReached = repo.metadata_status === 'limit_reached';
                return (
                  <div key={repo.repo_full_name} className="p-2 rounded-md border border-border space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-sm font-medium truncate">{repo.repo_full_name}</span>
                        {repo.is_private && <Badge variant="secondary" className="text-xs">Private</Badge>}
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
                                    delete next[repo.repo_full_name];
                                    return next;
                                  }
                                  return { ...prev, [repo.repo_full_name]: repo.metadata_summary || '' };
                                });
                              }}
                              title={isEditing ? 'Cancel edit' : 'Edit description'}
                              data-testid={`repo-edit-${repo.repo_full_name}`}
                            >
                              {isEditing ? <X className="h-3 w-3" /> : <Pencil className="h-3 w-3" />}
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-6 w-6 p-0"
                              onClick={() => handleRegenerate(repo.repo_full_name)}
                              title="Regenerate description"
                              data-testid={`repo-regenerate-${repo.repo_full_name}`}
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
                            onClick={() => handleRegenerate(repo.repo_full_name)}
                            data-testid={`repo-retry-${repo.repo_full_name}`}
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
                      <p className="text-xs text-red-500">{isLimitReached ? 'Usage limit reached — upgrade to continue.' : 'Failed to generate description'}</p>
                    )}
                    {isReady && isEditing && (
                      <div className="space-y-1">
                        <Textarea
                          value={editingMetadata[repo.repo_full_name]}
                          onChange={e => setEditingMetadata(prev => ({ ...prev, [repo.repo_full_name]: e.target.value }))}
                          className="text-xs min-h-[60px]"
                          rows={2}
                        />
                        <div className="flex gap-1 justify-end">
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 px-2 text-xs"
                            onClick={() => setEditingMetadata(prev => {
                              const next = { ...prev };
                              delete next[repo.repo_full_name];
                              return next;
                            })}
                          >
                            Cancel
                          </Button>
                          <Button
                            size="sm"
                            className="h-6 px-2 text-xs"
                            onClick={() => handleMetadataSave(repo.repo_full_name)}
                          >
                            Save
                          </Button>
                        </div>
                      </div>
                    )}
                    {isReady && !isEditing && repo.metadata_summary && (
                      <p className="text-xs text-muted-foreground">{repo.metadata_summary.replace(/\*\*/g, '')}</p>
                    )}
                    {repo.installation_id != null && authConfig.incident_prevention_enabled && (
                      <div
                        className="flex items-center justify-between gap-2 pt-1"
                        title="Incident Prevention — Aurora reviews PRs for incident risk"
                      >
                        <span className="text-xs text-muted-foreground">Incident Prevention</span>
                        <Switch
                          checked={!!repo.change_gating_enabled}
                          disabled={gatingUpdating.has(repo.repo_full_name)}
                          onCheckedChange={(checked) => handleChangeGatingToggle(repo.repo_full_name, checked)}
                          className="scale-75 origin-right"
                          aria-label={`Incident Prevention for ${repo.repo_full_name}`}
                          data-testid={`repo-change-gating-${repo.repo_full_name}`}
                        />
                      </div>
                    )}
                  </div>
                );
              })}
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

              {visibleRepos.length > 0 && (
                <label className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground cursor-pointer hover:text-foreground">
                  <Checkbox
                    data-testid="github-select-all"
                    checked={visibleRepos.length > 0 && visibleRepos.every(r => checkedRepos.has(r.full_name))}
                    onCheckedChange={(checked) => {
                      setCheckedRepos(prev => {
                        const next = new Set(prev);
                        if (checked) {
                          visibleRepos.forEach(r => next.add(r.full_name));
                        } else {
                          visibleRepos.forEach(r => next.delete(r.full_name));
                        }
                        return next;
                      });
                    }}
                  />
                  <span>
                    Select all
                    {searchFilter || installationFilter !== 'all' ? ' (filtered)' : ''}
                    {' · '}
                    {visibleRepos.length} repos
                  </span>
                </label>
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
                  disabled={isSaving || !hasUnsavedChanges}
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

      <AlertDialog
        open={showDisconnectDialog}
        onOpenChange={(open) => {
          if (isDisconnecting) return; // don't dismiss mid-request
          setShowDisconnectDialog(open);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect GitHub?</AlertDialogTitle>
            <AlertDialogDescription>
              Removes your connected repositories and Aurora&apos;s link to your GitHub
              {installations.length > 0 ? ' App installation' : ' account'}.
              You can reconnect at any time.
            </AlertDialogDescription>
          </AlertDialogHeader>

          {installations.length > 0 && (
            <label
              className="flex items-start gap-2 rounded-md border border-border bg-muted/30 p-3 text-sm cursor-pointer hover:bg-muted/50"
              data-testid="disconnect-also-uninstall-row"
            >
              <Checkbox
                checked={alsoUninstallOnGitHub}
                onCheckedChange={(checked) => setAlsoUninstallOnGitHub(checked === true)}
                disabled={isDisconnecting}
                className="mt-0.5"
                data-testid="disconnect-also-uninstall-checkbox"
              />
              <span className="leading-snug">
                <span className="font-medium text-foreground">Also uninstall the GitHub App</span>
                <span className="block text-xs text-muted-foreground mt-0.5">
                  Fully removes the App from
                  {installations.length === 1
                    ? <> <span className="font-medium">{installations[0].account_login}</span> on GitHub</>
                    : <> all {installations.length} linked GitHub accounts</>}
                  . If you skip this, the App stays installed and continues sending webhooks until you remove it from GitHub&apos;s settings.
                </span>
              </span>
            </label>
          )}

          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDisconnecting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault(); // keep the dialog open while we work
                handleDisconnect(alsoUninstallOnGitHub);
              }}
              disabled={isDisconnecting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              data-testid="disconnect-confirm"
            >
              {isDisconnecting ? <Loader2 className="h-3 w-3 animate-spin mr-2" /> : null}
              {alsoUninstallOnGitHub && installations.length > 0
                ? 'Disconnect & Uninstall'
                : 'Disconnect'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

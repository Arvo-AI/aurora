'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from '@/hooks/use-toast';
import { Loader2, Check, Pencil, RotateCw, X, RefreshCw, Info, Copy, AlertTriangle, CheckCircle2, Search } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { BitbucketIntegrationService } from '@/services/bitbucket-integration-service';
import type { Workspace, Repo, ChangeGatingResponse } from '@/services/bitbucket-integration-service';
import { isIncidentPreventionEnabled } from '@/lib/feature-flags';

interface ConnectedRepo {
  slug: string;
  full_name: string;
  workspace: string | null;
  default_branch: string | null;
  metadata_summary: string | null;
  metadata_status: string | null;
  change_gating_enabled: boolean;
  webhook_configured: boolean;
  webhook_stale: boolean;
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
      change_gating_enabled: !!r.change_gating_enabled,
      webhook_configured: !!r.webhook_configured,
      webhook_stale: !!r.webhook_stale,
    }));
}

export default function BitbucketWorkspaceBrowser() {
  const { toast } = useToast();

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string>('');
  const [isLoadingWorkspaces, setIsLoadingWorkspaces] = useState(false);

  const [repos, setRepos] = useState<Repo[]>([]);
  const [checkedRepos, setCheckedRepos] = useState<Set<string>>(new Set());
  const [searchFilter, setSearchFilter] = useState('');
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const isRestoringSelectionRef = useRef(false);
  const reposFetchGen = useRef(0);
  // Map of workspace → set of saved slugs (supports multi-workspace)
  const [savedReposByWorkspace, setSavedReposByWorkspace] = useState<Map<string, Set<string>>>(new Map());
  const savedReposByWorkspaceRef = useRef<Map<string, Set<string>>>(new Map());

  // Connected repos with metadata status (for the "Connected Repositories" section)
  const [savedRepos, setSavedRepos] = useState<ConnectedRepo[]>([]);
  const [editingMetadata, setEditingMetadata] = useState<Record<string, string>>({});
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Incident Prevention (PR change gating). The feature flag is read from
  // the injected env (instant) — not from /bitbucket/status, which does a
  // live Bitbucket API round-trip and left the toggle invisible for seconds.
  const incidentPreventionEnabled = isIncidentPreventionEnabled();
  const [gatingUpdating, setGatingUpdating] = useState<Set<string>>(new Set());
  const [bulkEnabling, setBulkEnabling] = useState<false | 'enable' | 'disable'>(false);
  const [webhookSetup, setWebhookSetup] = useState<ChangeGatingResponse | null>(null);
  const [isVerifying, setIsVerifying] = useState(false);

  const handleChangeGatingToggle = async (repoFullName: string, enabled: boolean) => {
    setGatingUpdating(prev => new Set(prev).add(repoFullName));
    try {
      const result = await BitbucketIntegrationService.updateChangeGating(repoFullName, enabled);
      setSavedRepos(prev => prev.map(r =>
        r.full_name === repoFullName
          ? { ...r, change_gating_enabled: enabled, webhook_configured: !!(enabled && result.webhook_auto_created), webhook_stale: false }
          : r
      ));
      if (enabled && result.webhook_url && result.webhook_auto_created === false) {
        setWebhookSetup(result);
      } else if (!enabled && webhookSetup?.repo_full_name === repoFullName) {
        setWebhookSetup(null);
      }
      if (!enabled) {
        toast({
          title: "Incident Prevention disabled",
          description: result.webhook_cleanup_failed
            ? "Aurora can't delete this webhook (Bitbucket only allows that for hooks it created). No PRs will be reviewed, but remove the webhook in Repository settings to stop Bitbucket sending events."
            : undefined,
        });
      }
    } catch (error: unknown) {
      const err = error as Error;
      toast({
        title: "Error",
        description: err.message || "Failed to update Incident Prevention setting",
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

  const handleBulkGating = async (enabled: boolean) => {
    const names = savedRepos
      .filter(r => r.workspace === selectedWorkspace && checkedRepos.has(r.slug) && r.change_gating_enabled !== enabled)
      .map(r => r.full_name);
    if (names.length === 0) {
      const checkedConnected = [...checkedRepos].some(s => savedReposByWorkspace.get(selectedWorkspace)?.has(s));
      toast({
        title: checkedConnected
          ? `Incident Prevention is already ${enabled ? 'on' : 'off'} for the selected connected repos`
          : 'Save selected repos first',
      });
      return;
    }
    setBulkEnabling(enabled ? 'enable' : 'disable');
    setGatingUpdating(new Set(names));
    try {
      const job = await BitbucketIntegrationService.updateChangeGatingBulk(names, enabled);
      let status = await BitbucketIntegrationService.getChangeGatingBulkJob(job.task_id);
      const deadline = Date.now() + 10 * 60 * 1000;
      while (!status.complete) {
        if (Date.now() > deadline) {
          throw new Error('Incident Prevention is taking longer than expected. Reload this page to see the current state.');
        }
        const data = await BitbucketIntegrationService.loadWorkspaceSelection();
        if (data?.repositories) {
          const updated = parseConnectedRepos(data.repositories);
          setSavedRepos(updated);
          setGatingUpdating(prev => {
            const next = new Set(prev);
            for (const r of updated) {
              if (!next.has(r.full_name)) continue;
              const settled = enabled
                ? r.webhook_configured || !r.change_gating_enabled
                : !r.change_gating_enabled;
              if (settled) next.delete(r.full_name);
            }
            return next;
          });
          // Disable is done once the DB flag is off. Hook deletes keep
          // running in the worker; the UI does not wait on them.
          if (!enabled && names.every(n => !updated.find(r => r.full_name === n)?.change_gating_enabled)) {
            break;
          }
        }
        await new Promise(r => setTimeout(r, 2000));
        status = await BitbucketIntegrationService.getChangeGatingBulkJob(job.task_id);
      }
      if (status.complete && status.error) throw new Error(status.status || 'Failed to update Incident Prevention');
      const result = status.result;
      const data = await BitbucketIntegrationService.loadWorkspaceSelection();
      if (data?.repositories) setSavedRepos(parseConnectedRepos(data.repositories));
      if (enabled) {
        const manual = result?.results.filter(r => r.webhook_auto_created === false) ?? [];
        if (manual.length && result?.webhook_url) {
          setWebhookSetup({
            repo_full_name: manual[0].repo_full_name,
            change_gating_enabled: true,
            webhook_url: result.webhook_url,
            webhook_secret: result.webhook_secret,
            webhook_events: result.webhook_events,
            webhook_auto_created: false,
            manual_count: manual.length,
          });
        } else {
          toast({ title: `Incident Prevention enabled on ${names.length} repo${names.length === 1 ? '' : 's'}` });
        }
        const skipped = result?.results.filter(r => r.error === 'no_default_branch').length ?? 0;
        if (skipped) {
          toast({
            title: `${skipped} repo${skipped === 1 ? '' : 's'} skipped`,
            description: 'No default branch recorded. Re-save the repository selection, then try again.',
            variant: 'destructive',
          });
        }
      } else {
        toast({
          title: `Incident Prevention disabled on ${names.length} repo${names.length === 1 ? '' : 's'}`,
        });
      }
    } catch (error: unknown) {
      const err = error as Error;
      toast({ title: 'Error', description: err.message || 'Failed to update Incident Prevention', variant: 'destructive' });
    } finally {
      setBulkEnabling(false);
      setGatingUpdating(new Set());
    }
  };

  const handleDisconnectSelected = async () => {
    if (!selectedWorkspace) return;
    const saved = savedReposByWorkspace.get(selectedWorkspace);
    if (!saved?.size) return;
    const remainingSlugs = [...saved].filter(s => !checkedRepos.has(s));
    const removed = saved.size - remainingSlugs.length;
    setIsSaving(true);
    try {
      await BitbucketIntegrationService.saveWorkspaceSelection({
        workspace: selectedWorkspace,
        repositories: remainingSlugs.map((slug): Repo => {
          const repo = repos.find(r => r.slug === slug);
          return repo ?? { slug, name: slug, full_name: `${selectedWorkspace}/${slug}` };
        }),
      });
      setCheckedRepos(new Set(remainingSlugs));
      setSavedReposByWorkspace(prev => {
        const next = new Map(prev);
        if (remainingSlugs.length) next.set(selectedWorkspace, new Set(remainingSlugs));
        else next.delete(selectedWorkspace);
        savedReposByWorkspaceRef.current = next;
        return next;
      });
      const data = await BitbucketIntegrationService.loadWorkspaceSelection();
      setSavedRepos(data?.repositories ? parseConnectedRepos(data.repositories) : []);
      window.dispatchEvent(new CustomEvent('providerStateChanged'));
      toast({ title: "Disconnected", description: `${removed} repo${removed === 1 ? '' : 's'} disconnected` });
    } catch (error: unknown) {
      const err = error as Error;
      toast({ title: "Error", description: err.message || "Failed to disconnect", variant: "destructive" });
    } finally {
      setIsSaving(false);
    }
  };

  const handleReopenSetup = async (repoFullName: string) => {
    // Re-enabling an already-enabled repo returns the same org webhook URL and
    // secret WITHOUT touching Bitbucket (the server skips hook creation when
    // the repo was already on), so the dialog can be reopened after dismissal
    // without this read-only action mutating remote state.
    setGatingUpdating(prev => new Set(prev).add(repoFullName));
    try {
      const result = await BitbucketIntegrationService.updateChangeGating(repoFullName, true);
      if (result.webhook_url) setWebhookSetup(result);
    } catch (error: unknown) {
      const err = error as Error;
      toast({ title: "Error", description: err.message || "Failed to load webhook setup", variant: "destructive" });
    } finally {
      setGatingUpdating(prev => {
        const next = new Set(prev);
        next.delete(repoFullName);
        return next;
      });
    }
  };

  const handleVerifyWebhook = async (repoFullName: string) => {
    setIsVerifying(true);
    try {
      const result = await BitbucketIntegrationService.verifyChangeGatingWebhook(repoFullName);
      if (result.verified) {
        setSavedRepos(prev => prev.map(r =>
          r.full_name === repoFullName ? { ...r, webhook_configured: true } : r
        ));
        setWebhookSetup(null);
        toast({ title: "Webhook verified", description: `Incident Prevention is active for ${repoFullName}.` });
      } else if (result.reason === 'cannot_list_hooks') {
        toast({
          title: "Verification pending",
          description: result.detail || "Aurora will confirm the hook automatically on the first pull request event.",
        });
      } else {
        // Definitive miss: the server cleared its verification, so clear the
        // badge too rather than leaving a green one the server disagrees with.
        setSavedRepos(prev => prev.map(r =>
          r.full_name === repoFullName ? { ...r, webhook_configured: false } : r
        ));
        toast({
          title: "Webhook not found",
          description: result.detail || "No matching webhook yet. Add it in Bitbucket → Repository settings → Webhooks, or open a PR and Aurora will confirm it automatically.",
          variant: "destructive",
        });
      }
    } catch (error: unknown) {
      const err = error as Error;
      toast({ title: "Error", description: err.message || "Failed to verify webhook", variant: "destructive" });
    } finally {
      setIsVerifying(false);
    }
  };

  const copyToClipboard = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast({ title: "Copied", description: `${label} copied to clipboard` });
    } catch {
      toast({ title: "Error", description: `Failed to copy ${label}`, variant: "destructive" });
    }
  };

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
      setSearchFilter('');
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
    const gen = ++reposFetchGen.current;
    setIsLoadingRepos(true);
    try {
      const data = await BitbucketIntegrationService.getRepos(workspace);
      if (gen !== reposFetchGen.current) return;
      const repoList = Array.isArray(data) ? data : data?.repositories;
      if (!Array.isArray(repoList)) throw new Error('Failed to fetch repositories');
      setRepos(repoList);
      // Use ref to always read the latest saved state (avoids stale closure)
      const saved = savedReposByWorkspaceRef.current.get(workspace);
      setCheckedRepos(saved ? new Set(saved) : new Set());
    } catch (error) {
      if (gen !== reposFetchGen.current) return;
      console.error('Error fetching repos:', error);
      setRepos([]);
      setCheckedRepos(new Set());
    } finally {
      if (gen === reposFetchGen.current) setIsLoadingRepos(false);
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

  // Re-check each enabled repo's hook against Bitbucket on card open. Stored
  // verification is only ever proof of a PAST success, so without this a repo
  // stays green after its hook is deleted or disabled in Bitbucket. Failures
  // are silent (no toast): this is a background refresh, and the badge itself
  // reports the result.
  const revalidateWebhooks = (connected: ConnectedRepo[]) => {
    const enabled = connected.filter(r => r.change_gating_enabled).map(r => r.full_name);
    if (!incidentPreventionEnabled || enabled.length === 0) return;
    // ponytail: browser ~6 connections/host. 50 parallel verifies starved GET /repos.
    void (async () => {
      const byName = new Map<string, boolean>();
      let i = 0;
      const worker = async () => {
        while (i < enabled.length) {
          const name = enabled[i++];
          try {
            const res = await BitbucketIntegrationService.verifyChangeGatingWebhook(name);
            byName.set(name, !!res.verified);
          } catch { /* background refresh; badge stays as last known */ }
        }
      };
      await Promise.all([worker(), worker()]);
      if (byName.size === 0) return;
      setSavedRepos(prev => prev.map(r =>
        byName.has(r.full_name)
          ? { ...r, webhook_configured: byName.get(r.full_name)!, webhook_stale: byName.get(r.full_name)! ? false : r.webhook_stale }
          : r
      ));
    })();
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

      const firstWorkspace = data.workspace || byWorkspace.keys().next().value;
      if (firstWorkspace) {
        isRestoringSelectionRef.current = true;
        setSelectedWorkspace(firstWorkspace);
        try {
          await fetchRepos(firstWorkspace);
        } finally {
          isRestoringSelectionRef.current = false;
        }
      }
      revalidateWebhooks(connected);
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

  const currentWorkspaceSaved = savedReposByWorkspace.get(selectedWorkspace);
  const selectionChanged = selectedWorkspace && (
    checkedRepos.size !== (currentWorkspaceSaved?.size ?? 0) ||
    [...checkedRepos].some(s => !currentWorkspaceSaved?.has(s))
  );
  const visibleRepos = searchFilter
    ? repos.filter(r => `${r.full_name} ${r.name}`.toLowerCase().includes(searchFilter.toLowerCase()))
    : repos;
  const allVisibleChecked = visibleRepos.length > 0 && visibleRepos.every(r => checkedRepos.has(r.slug));
  const bulkBusy = !!bulkEnabling;
  const checkedConnected = savedRepos.filter(
    r => r.workspace === selectedWorkspace && checkedRepos.has(r.slug)
  );
  const canEnableGating = checkedConnected.some(r => !r.change_gating_enabled);
  const canDisableGating = checkedConnected.some(r => r.change_gating_enabled);

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
            <div className="space-y-1">
              <div className="relative">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground" />
                <Input
                  placeholder="Filter repositories..."
                  value={searchFilter}
                  onChange={(e) => setSearchFilter(e.target.value)}
                  className="h-8 text-xs pl-7"
                />
              </div>
              {visibleRepos.length > 0 && (
                <label className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground cursor-pointer hover:text-foreground">
                  <Checkbox
                    data-testid="bb-select-all"
                    checked={allVisibleChecked}
                    onCheckedChange={(checked) => {
                      setCheckedRepos(prev => {
                        const next = new Set(prev);
                        if (checked) visibleRepos.forEach(r => next.add(r.slug));
                        else visibleRepos.forEach(r => next.delete(r.slug));
                        return next;
                      });
                    }}
                  />
                  <span>
                    Select all
                    {searchFilter ? ' (filtered)' : ''}
                    {' · '}
                    {visibleRepos.length} repos
                  </span>
                </label>
              )}
              <div className="space-y-1 max-h-48 overflow-y-auto border border-border rounded-lg p-2">
                {visibleRepos.length > 0 ? visibleRepos.map((repo) => (
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
                )) : (
                  <p className="text-xs text-muted-foreground text-center py-4">No repositories match your filter.</p>
                )}
              </div>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No repositories found in this workspace.</p>
          )}
        </div>
      )}

      {selectedWorkspace && repos.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <Button
            size="sm"
            onClick={handleSave}
            disabled={isSaving || bulkBusy || checkedRepos.size === 0 || !selectionChanged}
          >
            {isSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" /> : null}
            Save
          </Button>
          {!!currentWorkspaceSaved?.size && [...checkedRepos].some(s => currentWorkspaceSaved.has(s)) && (
            <Button size="sm" variant="outline" onClick={handleDisconnectSelected} disabled={isSaving || bulkBusy}>
              Disconnect
            </Button>
          )}
          {incidentPreventionEnabled && checkedRepos.size > 0 && (
            <>
              {(canEnableGating || bulkEnabling === 'enable') && (
              <Button
                size="sm"
                variant="outline"
                disabled={isSaving || bulkBusy}
                onClick={() => handleBulkGating(true)}
                data-testid="bb-enable-gating-selected"
              >
                {bulkEnabling === 'enable' ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" /> : null}
                Enable Incident Prevention
              </Button>
              )}
              {(canDisableGating || bulkEnabling === 'disable') && (
              <Button
                size="sm"
                variant="outline"
                disabled={isSaving || bulkBusy}
                onClick={() => handleBulkGating(false)}
                data-testid="bb-disable-gating-selected"
              >
                {bulkEnabling === 'disable' ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" /> : null}
                Disable Incident Prevention
              </Button>
              )}
            </>
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
            const isGatingUpdating = gatingUpdating.has(repo.full_name);
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
                {incidentPreventionEnabled && (
                  <div className="flex items-center justify-between gap-2 pt-1">
                    <div className="flex items-center gap-2">
                      <TooltipProvider delayDuration={0}>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <button type="button" className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors">
                              Incident Prevention
                              <Info className="h-3 w-3" />
                            </button>
                          </TooltipTrigger>
                          <TooltipContent side="right" className="max-w-xs">
                            <p>Aurora reviews pull requests targeting this repo&apos;s default branch and flags changes that could cause production incidents. Requires a Bitbucket webhook (setup details appear when enabled).</p>
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                      {repo.change_gating_enabled && repo.webhook_configured && !repo.webhook_stale && (
                        <Badge variant="outline" className="text-xs text-green-600 border-green-600/40 gap-1 px-1.5">
                          <CheckCircle2 className="h-3 w-3" /> Active
                        </Badge>
                      )}
                      {/* Stale wins over configured: a hook Aurora created is
                          tracked by uuid and so counts as configured, but if it
                          was verified at a different URL it can no longer reach
                          us and must not read as healthy. */}
                      {repo.change_gating_enabled && (!repo.webhook_configured || repo.webhook_stale) && (
                        <button
                          type="button"
                          className="inline-flex disabled:opacity-50"
                          disabled={isGatingUpdating || bulkBusy}
                          onClick={() => handleReopenSetup(repo.full_name)}
                          title={repo.webhook_stale
                            ? "This repo's webhook points at a different Aurora URL and can no longer reach us — click to get the current URL and secret"
                            : "Waiting for the first pull request event from Bitbucket — click to view the webhook URL and secret"}
                        >
                          {repo.webhook_stale ? (
                            <Badge variant="outline" className="text-xs text-red-600 border-red-600/40 gap-1 px-1.5 cursor-pointer">
                              <AlertTriangle className="h-3 w-3" /> Webhook URL changed
                            </Badge>
                          ) : (
                            <Badge variant="outline" className="text-xs text-amber-600 border-amber-600/40 gap-1 px-1.5 cursor-pointer">
                              <AlertTriangle className="h-3 w-3" /> Awaiting first delivery
                            </Badge>
                          )}
                        </button>
                      )}
                    </div>
                    {isGatingUpdating ? (
                      <Loader2 className="h-4 w-4 animate-spin text-muted-foreground flex-shrink-0" />
                    ) : (
                      <Switch
                        checked={repo.change_gating_enabled}
                        disabled={bulkBusy}
                        onCheckedChange={(checked) => handleChangeGatingToggle(repo.full_name, checked)}
                        className="scale-75 origin-right"
                        aria-label={`Incident Prevention for ${repo.full_name}`}
                        data-testid={`bb-repo-change-gating-${repo.full_name}`}
                      />
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <Dialog open={!!webhookSetup?.webhook_url} onOpenChange={(open) => { if (!open) setWebhookSetup(null); }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              Webhook setup — {(webhookSetup?.manual_count ?? 0) > 1
                ? `${webhookSetup!.manual_count} repos`
                : webhookSetup?.repo_full_name}
            </DialogTitle>
            <DialogDescription>
              {webhookSetup?.webhook_auto_created === undefined
                ? 'Incident Prevention is already on for this repository. Below are the webhook details for it.'
                : webhookSetup.webhook_auto_created
                ? 'Aurora created the webhook on this repository, so there is nothing to paste. Click Verify to confirm it.'
                : (webhookSetup?.manual_count ?? 0) > 1
                ? `Aurora could not create the webhook automatically on ${webhookSetup!.manual_count} repos (that needs the write:webhook:bitbucket scope). Add it in Bitbucket using the URL and secret below.`
                : 'Aurora could not create the webhook automatically (that needs the write:webhook:bitbucket scope). Add it in Bitbucket using the URL and secret below.'}
            </DialogDescription>
          </DialogHeader>
          {!webhookSetup?.webhook_auto_created && (
            <p className="text-xs text-muted-foreground">
              In Bitbucket, go to <span className="font-medium">Repository settings → Webhooks → Add webhook</span> and
              paste the URL and secret below, with triggers <span className="font-mono">Pull request: Created</span> and{' '}
              <span className="font-mono">Pull request: Updated</span>. Bitbucket sends no test event, so Aurora confirms
              the hook on the first pull request — open or update a PR to activate it
              {(webhookSetup?.manual_count ?? 0) > 1 ? '.' : ', or click Verify to check now.'}
            </p>
          )}
          <p className="text-xs text-muted-foreground font-medium">
            {webhookSetup?.webhook_auto_created
              ? 'Keep these: the same URL and secret apply to every repository you enable, and you will need them if this webhook is ever removed.'
              : 'Use this SAME URL and secret on every repository you enable — the secret is shared across your organization.'}
          </p>
          <div className="space-y-2">
            <div className="flex items-center gap-1">
              <span className="text-xs text-muted-foreground w-12 flex-shrink-0">URL</span>
              <code className="text-xs bg-muted border border-border rounded px-2 py-1 flex-1 break-all">{webhookSetup?.webhook_url}</code>
              <Button variant="ghost" size="sm" className="h-6 w-6 p-0 flex-shrink-0" title="Copy webhook URL"
                onClick={() => copyToClipboard(webhookSetup!.webhook_url!, 'Webhook URL')}>
                <Copy className="h-3 w-3" />
              </Button>
            </div>
            <div className="flex items-center gap-1">
              <span className="text-xs text-muted-foreground w-12 flex-shrink-0">Secret</span>
              <code className="text-xs bg-muted border border-border rounded px-2 py-1 flex-1 break-all">{webhookSetup?.webhook_secret}</code>
              <Button variant="ghost" size="sm" className="h-6 w-6 p-0 flex-shrink-0" title="Copy webhook secret"
                onClick={() => copyToClipboard(webhookSetup!.webhook_secret!, 'Webhook secret')}>
                <Copy className="h-3 w-3" />
              </Button>
            </div>
          </div>
          <DialogFooter className="gap-2">
            <Button variant="outline" size="sm" onClick={() => setWebhookSetup(null)}>
              Later
            </Button>
            {(webhookSetup?.manual_count ?? 0) <= 1 && (
              <Button size="sm" disabled={isVerifying}
                onClick={() => webhookSetup && handleVerifyWebhook(webhookSetup.repo_full_name)}>
                {isVerifying ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" /> : null}
                Verify webhook
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

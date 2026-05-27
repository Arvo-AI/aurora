'use client';

import { useState, useEffect, useMemo } from 'react';
import Link from 'next/link';
import { Button } from "@/components/ui/button";
import { useToast } from '@/hooks/use-toast';
import { Loader2, RefreshCw, Settings } from 'lucide-react';
import { ProjectListItem } from '@/components/cloud-provider/ui/ProjectListItem';
import { fetchProjects, saveProjects } from '@/components/cloud-provider/projects/projectUtils';
import { Project } from '@/components/cloud-provider/types';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';
import { useConnectedAccounts } from '@/hooks/useConnectedAccounts';

interface GcpProviderIntegrationProps {
  onDisconnect?: () => void;
}

interface GcpAccountSummary {
  email?: string;
  project_id?: string;
  alias?: string;
  authType?: string;
  accessibleProjectIds?: string[];
}

interface GcpProviderEntry {
  count?: number;
  accounts?: GcpAccountSummary[];
  authType?: string;
}

export default function GcpProviderIntegration({ onDisconnect: _onDisconnect }: GcpProviderIntegrationProps) {
  const [userId, setUserId] = useState<string | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [togglingProjectId, setTogglingProjectId] = useState<string | null>(null);
  const { toast } = useToast();

  const { accounts } = useConnectedAccounts();
  const gcpEntry = accounts.gcp as GcpProviderEntry | undefined;
  const gcpAccounts: GcpAccountSummary[] = useMemo(
    () => (Array.isArray(gcpEntry?.accounts) ? gcpEntry!.accounts : []),
    [gcpEntry],
  );
  const authType: 'oauth' | 'service_account' | null =
    gcpEntry?.authType === 'service_account' || gcpEntry?.authType === 'oauth'
      ? gcpEntry.authType
      : null;

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

  // Fetch projects when userId is available
  useEffect(() => {
    if (userId) {
      loadProjects(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  const loadProjects = async (forceRefresh = false) => {
    setIsLoading(true);
    try {
      const fetchedProjects = await fetchProjects('gcp', forceRefresh, projects);
      setProjects(fetchedProjects);
    } catch (error: any) {
      console.error('Error loading GCP projects:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to load GCP projects",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  };

  const handleToggle = async (projectId: string) => {
    setTogglingProjectId(projectId);
    try {
      const updatedProjects = projects.map(p =>
        p.projectId === projectId ? { ...p, enabled: !p.enabled } : p
      );
      setProjects(updatedProjects);

      await saveProjects('gcp', updatedProjects);

      toast({
        title: "Success",
        description: `Project ${updatedProjects.find(p => p.projectId === projectId)?.enabled ? 'enabled' : 'disabled'}`,
      });
    } catch (error: any) {
      console.error('Error toggling project:', error);
      loadProjects();
      toast({
        title: "Error",
        description: error.message || "Failed to update project",
        variant: "destructive",
      });
    } finally {
      setTogglingProjectId(null);
    }
  };

  const handleSetAsRoot = async (_providerId: string, projectId: string) => {
    if (!userId) return;

    setIsSaving(true);
    try {
      const response = await fetch('/api/root-project', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ projectId }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Failed to set root project');
      }

      await loadProjects();

      toast({
        title: "Success",
        description: "Root project updated successfully",
      });
    } catch (error: any) {
      console.error('Error setting root project:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to set root project",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  // Group projects by service account using accessibleProjectIds
  const grouped = useMemo(() => {
    if (gcpAccounts.length === 0) {
      return [{ key: '__all__', sa: null as GcpAccountSummary | null, projects }];
    }
    const projectIdToAccount: Record<string, GcpAccountSummary> = {};
    for (const sa of gcpAccounts) {
      for (const pid of sa.accessibleProjectIds ?? []) {
        if (!projectIdToAccount[pid]) projectIdToAccount[pid] = sa;
      }
    }
    const groups: Array<{ key: string; sa: GcpAccountSummary | null; projects: Project[] }> = gcpAccounts.map((sa, idx) => ({
      // Stable key: prefer email, then project_id, then a deterministic index
      // fallback. Math.random() here would change on every useMemo recompute
      // and force React to unmount/remount the whole account block.
      key: sa.email ?? sa.project_id ?? `gcp-account-${idx}`,
      sa,
      projects: [],
    }));
    const ungrouped: Project[] = [];
    for (const project of projects) {
      const owner = projectIdToAccount[project.projectId];
      const group = owner
        ? groups.find(g => g.sa?.email === owner.email)
        : undefined;
      if (group) group.projects.push(project);
      else ungrouped.push(project);
    }
    if (ungrouped.length > 0) {
      groups.push({ key: '__other__', sa: null, projects: ungrouped });
    }
    return groups.filter(g => g.projects.length > 0);
  }, [projects, gcpAccounts]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
        <span className="ml-2 text-sm text-muted-foreground">Loading projects...</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-lg font-semibold">GCP Projects</h3>
            {authType && (
              <Badge variant="secondary" className="text-xs">
                {authType === 'service_account' ? 'Service Account' : 'OAuth'}
              </Badge>
            )}
            {typeof gcpEntry?.count === 'number' && gcpEntry.count > 0 && (
              <Badge variant="outline" className="text-xs">
                {gcpEntry.count} account{gcpEntry.count === 1 ? '' : 's'}
              </Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            Manage which projects each service account can access
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          <Button
            variant="outline"
            size="sm"
            onClick={() => loadProjects(true)}
            disabled={isLoading}
          >
            <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
          <Button asChild variant="outline" size="sm">
            <Link href="/gcp/auth">
              <Settings className="h-4 w-4 mr-2" />
              Manage Service Accounts
            </Link>
          </Button>
        </div>
      </div>

      {projects.length === 0 ? (
        <div className="text-center py-8 text-muted-foreground">
          <p>No GCP projects found.</p>
        </div>
      ) : (
        <ScrollArea className="h-[400px] pr-4">
          <div className="space-y-6">
            {grouped.map(group => (
              <div key={group.key} className="space-y-2">
                {group.sa && (
                  <div className="flex flex-col gap-0.5 px-1">
                    <div className="text-sm font-medium">
                      {group.sa.alias || group.sa.email || 'Service Account'}
                    </div>
                    {group.sa.alias && group.sa.email && (
                      <div className="text-xs text-muted-foreground truncate">
                        {group.sa.email}
                      </div>
                    )}
                  </div>
                )}
                {group.key === '__other__' && (
                  <div className="text-xs text-muted-foreground px-1">
                    Other projects
                  </div>
                )}
                <div className="space-y-2">
                  {group.projects.map(project => (
                    <ProjectListItem
                      key={project.projectId}
                      project={project}
                      providerId="gcp"
                      isLoading={togglingProjectId === project.projectId || isSaving}
                      onToggle={handleToggle}
                      onSetAsRoot={handleSetAsRoot}
                      showToggle={true}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </ScrollArea>
      )}
    </div>
  );
}

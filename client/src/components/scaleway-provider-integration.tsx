'use client';

import { useState, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { useToast } from '@/hooks/use-toast';
import { Loader2, RefreshCw, LogOut } from 'lucide-react';
import { ProjectListItem } from '@/components/cloud-provider/ui/ProjectListItem';
import { fetchProjects, saveProjects } from '@/components/cloud-provider/projects/projectUtils';
import { Project } from '@/components/cloud-provider/types';
import { ScrollArea } from '@/components/ui/scroll-area';

interface ScalewayProviderIntegrationProps {
  onDisconnect?: () => void;
}

export default function ScalewayProviderIntegration({ onDisconnect }: ScalewayProviderIntegrationProps) {
  const [userId, setUserId] = useState<string | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [togglingProjectId, setTogglingProjectId] = useState<string | null>(null);
  const { toast } = useToast();

  useEffect(() => {
    async function fetchUserId(): Promise<void> {
      try {
        const response = await fetch('/api/getUserId');
        if (response.ok) {
          const data = await response.json();
          setUserId(data.userId);
        }
      } catch (error) {
        console.error('Error fetching user ID:', error);
      }
    }
    fetchUserId();
  }, []);

  useEffect(() => {
    if (userId) {
      loadProjects();
    }
  }, [userId]);

  async function loadProjects(forceRefresh = false): Promise<void> {
    setIsLoading(true);
    try {
      const fetchedProjects = await fetchProjects('scaleway', forceRefresh, projects);
      setProjects(fetchedProjects);
    } catch (error: any) {
      console.error('Error loading Scaleway projects:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to load Scaleway projects",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }

  async function handleToggle(projectId: string): Promise<void> {
    setTogglingProjectId(projectId);
    try {
      const updatedProjects = projects.map(p =>
        p.projectId === projectId ? { ...p, enabled: !p.enabled } : p
      );
      setProjects(updatedProjects);

      await saveProjects('scaleway', updatedProjects);

      const toggledProject = updatedProjects.find(p => p.projectId === projectId);
      toast({
        title: "Success",
        description: `Project ${toggledProject?.enabled ? 'enabled' : 'disabled'}`,
      });
    } catch (error: any) {
      console.error('Error toggling project:', error);
      loadProjects(true);
      toast({
        title: "Error",
        description: error.message || "Failed to update project",
        variant: "destructive",
      });
    } finally {
      setTogglingProjectId(null);
    }
  }

  async function handleSetAsRoot(_providerId: string, projectId: string): Promise<void> {
    if (!userId) return;

    try {
      const response = await fetch('/api/provider-root-project/scaleway', {
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

      await loadProjects(true);

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
    }
  }

  async function handleDisconnect(): Promise<void> {
    if (!userId) return;

    setIsDisconnecting(true);
    try {
      const response = await fetch('/api/connected-accounts/scaleway', {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Failed to disconnect Scaleway');
      }

      setProjects([]);

      if (typeof window !== 'undefined') {
        localStorage.removeItem('isScalewayConnected');
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
      }

      toast({
        title: "Disconnected",
        description: "Scaleway account disconnected successfully",
      });

      onDisconnect?.();
    } catch (error: any) {
      console.error('Error disconnecting Scaleway:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to disconnect Scaleway",
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
    }
  }

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
          <h3 className="text-lg font-semibold">Scaleway Projects</h3>
          <p className="text-sm text-muted-foreground">
            Manage which projects Aurora can access
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => loadProjects(true)}
            disabled={isLoading}
          >
            <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={handleDisconnect}
            disabled={isDisconnecting}
          >
            {isDisconnecting ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Disconnecting...
              </>
            ) : (
              <>
                <LogOut className="h-4 w-4 mr-2" />
                Disconnect
              </>
            )}
          </Button>
        </div>
      </div>

      {projects.length === 0 ? (
        <div className="text-center py-8 text-muted-foreground">
          <p>No Scaleway projects found.</p>
          <p className="text-sm mt-2">Make sure you have projects in your Scaleway account.</p>
        </div>
      ) : (
        <ScrollArea className="h-[400px] pr-4">
          <div className="space-y-2">
            {projects.map((project) => (
              <ProjectListItem
                key={project.projectId}
                project={project}
                providerId="scaleway"
                isLoading={togglingProjectId === project.projectId}
                onToggle={handleToggle}
                onSetAsRoot={handleSetAsRoot}
                showToggle={true}
              />
            ))}
          </div>
        </ScrollArea>
      )}
    </div>
  );
}

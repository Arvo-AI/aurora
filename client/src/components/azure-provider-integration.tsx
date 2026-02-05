'use client';

import { useState, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { useToast } from '@/hooks/use-toast';
import { Loader2, RefreshCw, LogOut } from 'lucide-react';
import { ProjectListItem } from '@/components/cloud-provider/ui/ProjectListItem';
import { fetchProjects, saveProjects } from '@/components/cloud-provider/projects/projectUtils';
import { Project } from '@/components/cloud-provider/types';
import { ScrollArea } from '@/components/ui/scroll-area';

interface AzureProviderIntegrationProps {
  onDisconnect?: () => void;
}

export default function AzureProviderIntegration({ onDisconnect }: AzureProviderIntegrationProps) {
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
      const fetchedProjects = await fetchProjects('azure', forceRefresh, projects);
      setProjects(fetchedProjects);
    } catch (error: any) {
      console.error('Error loading Azure subscriptions:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to load Azure subscriptions",
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

      await saveProjects('azure', updatedProjects);

      const toggledProject = updatedProjects.find(p => p.projectId === projectId);
      toast({
        title: "Success",
        description: `Subscription ${toggledProject?.enabled ? 'enabled' : 'disabled'}`,
      });
    } catch (error: any) {
      console.error('Error toggling subscription:', error);
      loadProjects(true);
      toast({
        title: "Error",
        description: error.message || "Failed to update subscription",
        variant: "destructive",
      });
    } finally {
      setTogglingProjectId(null);
    }
  }

  async function handleSetAsRoot(providerId: string, projectId: string): Promise<void> {
    if (!userId) return;

    try {
      const response = await fetch('/api/provider-root-project/azure', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ projectId }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Failed to set root subscription');
      }

      await loadProjects(true);

      toast({
        title: "Success",
        description: "Root subscription updated successfully",
      });
    } catch (error: any) {
      console.error('Error setting root subscription:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to set root subscription",
        variant: "destructive",
      });
    }
  }

  async function handleDisconnect(): Promise<void> {
    if (!userId) return;

    setIsDisconnecting(true);
    try {
      const response = await fetch('/api/connected-accounts/azure', {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Failed to disconnect Azure');
      }

      setProjects([]);

      if (typeof window !== 'undefined') {
        localStorage.removeItem('isAzureConnected');
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
      }

      toast({
        title: "Disconnected",
        description: "Azure account disconnected successfully",
      });

      onDisconnect?.();
    } catch (error: any) {
      console.error('Error disconnecting Azure:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to disconnect Azure",
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
        <span className="ml-2 text-sm text-muted-foreground">Loading subscriptions...</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">Azure Subscriptions</h3>
          <p className="text-sm text-muted-foreground">
            Manage which Azure subscriptions Aurora can access
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
          <p>No Azure subscriptions found.</p>
          <p className="text-sm mt-2">Make sure you have subscriptions connected in your Azure account.</p>
        </div>
      ) : (
        <ScrollArea className="h-[400px] pr-4">
          <div className="space-y-2">
            {projects.map((project) => (
              <ProjectListItem
                key={project.projectId}
                project={project}
                providerId="azure"
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

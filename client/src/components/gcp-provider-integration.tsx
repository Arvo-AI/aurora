'use client';

import { useState, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { useToast } from '@/hooks/use-toast';
import { Loader2, RefreshCw, LogOut, RefreshCcw } from 'lucide-react';
import { ProjectListItem } from '@/components/cloud-provider/ui/ProjectListItem';
import { fetchProjects, saveProjects, ProjectCache } from '@/components/cloud-provider/projects/projectUtils';
import { Project } from '@/components/cloud-provider/types';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';
import { getEnv } from '@/lib/env';
import { useConnectedAccounts } from '@/hooks/useConnectedAccounts';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

interface GcpProviderIntegrationProps {
  onDisconnect?: () => void;
  onSwitchAuthMethod?: () => void;
}

export default function GcpProviderIntegration({ onDisconnect, onSwitchAuthMethod }: GcpProviderIntegrationProps) {
  const [userId, setUserId] = useState<string | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [togglingProjectId, setTogglingProjectId] = useState<string | null>(null);
  const [showSwitchConfirm, setShowSwitchConfirm] = useState(false);
  const { toast } = useToast();

  const { accounts } = useConnectedAccounts();
  const rawAuthType = (accounts.gcp as { authType?: string } | undefined)?.authType;
  const authType: 'oauth' | 'service_account' | null =
    rawAuthType === 'service_account' || rawAuthType === 'oauth' ? rawAuthType : null;

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
      // Update local state immediately for better UX
      const updatedProjects = projects.map(p =>
        p.projectId === projectId ? { ...p, enabled: !p.enabled } : p
      );
      setProjects(updatedProjects);

      // Save to backend
      await saveProjects('gcp', updatedProjects);
      
      toast({
        title: "Success",
        description: `Project ${updatedProjects.find(p => p.projectId === projectId)?.enabled ? 'enabled' : 'disabled'}`,
      });
    } catch (error: any) {
      console.error('Error toggling project:', error);
      // Revert on error
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

  const handleSetAsRoot = async (providerId: string, projectId: string) => {
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

      // Refresh projects to get updated root project status
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

  const handleDisconnect = async () => {
    if (!userId) return;
    
    setIsDisconnecting(true);
    try {
      const response = await fetch('/api/connected-accounts/gcp', {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Failed to disconnect GCP');
      }

      // Clear local state
      setProjects([]);
      ProjectCache.invalidate('gcp');
      // Notify other components to refresh their status
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
      }
      
      toast({
        title: "Disconnected",
        description: "GCP account disconnected successfully",
      });

      // Close dialog if callback provided
      if (onDisconnect) {
        onDisconnect();
      }
    } catch (error: any) {
      console.error('Error disconnecting GCP:', error);
      toast({
        title: "Error",
        description: error.message || "Failed to disconnect GCP",
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
    }
  };

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
          </div>
          <p className="text-sm text-muted-foreground">
            Manage which projects your service account can access
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
          {onSwitchAuthMethod && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowSwitchConfirm(true)}
              disabled={isDisconnecting}
            >
              <RefreshCcw className="h-4 w-4 mr-2" />
              Switch auth method
            </Button>
          )}
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

      <AlertDialog open={showSwitchConfirm} onOpenChange={setShowSwitchConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Switch GCP authentication method?</AlertDialogTitle>
            <AlertDialogDescription>
              You already have GCP connected. Continuing will replace the
              existing connection with the new authentication method. Your
              project selections may need to be re-applied.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setShowSwitchConfirm(false);
                if (onSwitchAuthMethod) onSwitchAuthMethod();
              }}
            >
              Continue
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {projects.length === 0 ? (
        <div className="text-center py-8 text-muted-foreground">
          <p>No GCP projects found.</p>
        </div>
      ) : (
        <ScrollArea className="h-[400px] pr-4">
          <div className="space-y-2">
            {projects.map((project) => (
              <ProjectListItem
                key={project.projectId}
                project={project}
                providerId="gcp"
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

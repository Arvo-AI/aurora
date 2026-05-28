"use client";

import React from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Loader2, Crown } from "lucide-react";
import { Project } from "../types";

interface ProjectListItemProps {
  project: Project;
  providerId: string;
  isLoading?: boolean;
  onToggle?: (projectId: string) => void;
  onSetAsRoot?: (providerId: string, projectId: string) => void;
  showToggle?: boolean;
}

export const ProjectListItem: React.FC<ProjectListItemProps> = ({
  project,
  providerId,
  isLoading = false,
  onToggle,
  onSetAsRoot,
  showToggle = true,
}) => {
  // Allow root project selection for GCP, OVH, and Scaleway
  const canSetAsRoot =
    (providerId === 'gcp' || providerId === 'ovh' || providerId === 'scaleway') &&
    !project.isRootProject &&
    project.hasPermission !== false &&
    onSetAsRoot;

  return (
    <div
      className={`flex items-center justify-between p-2 rounded-md border ${
        project.enabled ? 'border-primary/50 bg-primary/5' :
        project.hasPermission === false ? 'border-border bg-muted/30 opacity-60' : 'border-border'
      }`}
    >
      <div className="flex flex-col flex-1">
        <div className="flex items-center gap-2">
          <span className={`text-sm font-medium ${project.hasPermission === false ? 'text-muted-foreground' : ''}`}>
            {project.name || project.projectId}
          </span>
          {project.isRootProject && (providerId === 'gcp' || providerId === 'ovh' || providerId === 'scaleway') && (
            <Badge variant="default" className="text-xs px-2 py-0 flex items-center gap-1">
              <Crown className="w-3 h-3" />
              Root Project
            </Badge>
          )}
        </div>
        {project.name !== project.projectId && (
          <span className="text-xs text-muted-foreground">{project.projectId}</span>
        )}
        {project.hasPermission === false && (
          <span className="text-xs text-red-500 mt-1">No IAM permission</span>
        )}
        {project.isRootProject && (providerId === 'gcp' || providerId === 'ovh' || providerId === 'scaleway') && (
          <span className="text-xs text-muted-foreground mt-1">
            {providerId === 'gcp' ? 'Service accounts will be created in this project' : 
             providerId === 'ovh' ? 'Default project for OVH operations' :
             'Default project for Scaleway operations'}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2">
        {canSetAsRoot && (
          <Button
            variant="outline"
            size="sm"
            className="text-xs h-7 px-2"
            onClick={() => onSetAsRoot(providerId, project.projectId)}
            title="Set as root project for service account creation"
          >
            Set as Root
          </Button>
        )}

        {isLoading ? (
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
        ) : !showToggle ? null : (
          <Switch
            checked={project.enabled}
            disabled={project.hasPermission === false}
            onCheckedChange={() => onToggle?.(project.projectId)}
          />
        )}
      </div>
    </div>
  );
};
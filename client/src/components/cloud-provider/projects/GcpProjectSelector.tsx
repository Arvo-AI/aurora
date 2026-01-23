"use client";

import React from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

interface GcpProject {
  projectId: string;
  name: string;
  projectNumber: string;
  lifecycleState: string;
  billingEnabled: boolean;
  available: boolean;
}

interface GcpProjectSelectorProps {
  isLoading: boolean;
  error: string | null;
  projects: GcpProject[];
  selectedProjectId: string;
  onSelectProject: (id: string) => void;
  onRetry: () => void;
  className?: string;
}

const GcpProjectSelector: React.FC<GcpProjectSelectorProps> = ({
  isLoading,
  error,
  projects,
  selectedProjectId,
  onSelectProject,
  onRetry,
  className,
}) => {
  return (
    <div className={className ?? ""}>
      <h3 className="text-lg font-medium mb-4 text-foreground">GCP Project Selection</h3>
      <div className="space-y-4">
        <div className="flex items-center space-x-2">
          <Label htmlFor="project-selector" className="w-24 text-foreground">
            Project:
          </Label>
          <div className="flex-1">
            {isLoading ? (
              <div className="flex items-center space-x-2 text-sm text-muted-foreground">
                <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-primary"></div>
                <span>Loading projects...</span>
              </div>
            ) : error ? (
              <div className="text-sm text-red-500">
                {error}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={onRetry}
                  className="ml-2"
                >
                  Retry
                </Button>
              </div>
            ) : (
              <select
                id="project-selector"
                value={selectedProjectId}
                onChange={(e) => onSelectProject(e.target.value)}
                className="w-full rounded-md border border-input bg-muted px-3 py-2 text-sm text-foreground ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <option value="">Select a project...</option>
                {projects.map((project) => (
                  <option
                    key={project.projectId}
                    value={project.projectId}
                    disabled={!project.available}
                    style={{
                      color: project.available ? "inherit" : "#999",
                      fontStyle: project.available ? "normal" : "italic",
                    }}
                  >
                    {project.name} ({project.projectId})
                    {!project.billingEnabled && " - Billing Required"}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>
        {selectedProjectId && (
          <div className="text-sm text-muted-foreground">
            Selected: {projects.find((p) => p.projectId === selectedProjectId)?.name || selectedProjectId}
          </div>
        )}
        {projects.length > 0 && (
          <div className="text-xs text-muted-foreground">
            {projects.filter((p) => p.billingEnabled).length} of {projects.length} projects have billing enabled
          </div>
        )}
      </div>
    </div>
  );
};

export default GcpProjectSelector; 
import React from "react";
import { CardDescription } from "@/components/ui/card";
import { Loader2 } from "lucide-react";

/**
 * Props for ConnectorCardContent component.
 * Note: This is a generic component used for all connectors.
 * - For Slack: Shows Slack-specific connection details (workspace, team ID, channel) when slackStatus is provided
 * - For all other connectors: Simply displays the connector description
 */
interface ConnectorCardContentProps {
  isLoading: boolean;
  slackStatus: any; // Slack-specific status object (only used when connector.id === "slack")
  description: string; // Generic description shown for all connectors when slackStatus is null
}

export function ConnectorCardContent({ isLoading, slackStatus, description }: ConnectorCardContentProps) {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span className="text-sm">Loading connection details...</span>
      </div>
    );
  }

  if (slackStatus) {
    return (
      <div className="space-y-2 py-1">
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground font-medium">Workspace:</span>
          {slackStatus.team_url ? (
            <a 
              href={slackStatus.team_url} 
              target="_blank" 
              rel="noopener noreferrer"
              className="text-sm font-semibold text-primary hover:underline"
            >
              {slackStatus.team_name || 'Slack Workspace'}
            </a>
          ) : (
            <span className="text-sm font-semibold text-foreground">
              {slackStatus.team_name || 'Slack Workspace'}
            </span>
          )}
        </div>
        {slackStatus.team_id && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground font-medium">Team ID:</span>
            <span className="text-sm text-muted-foreground">
              {slackStatus.team_id}
            </span>
          </div>
        )}
        {slackStatus.incidents_channel_name && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground font-medium">Notification Channel:</span>
            <span className="text-sm font-semibold text-foreground">
              #{slackStatus.incidents_channel_name}
            </span>
          </div>
        )}
      </div>
    );
  }

  return <CardDescription className="text-sm leading-relaxed">{description}</CardDescription>;
}

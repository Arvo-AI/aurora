import React from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import GitHubProviderIntegration from "@/components/github-provider-integration";
import GcpProviderIntegration from "@/components/gcp-provider-integration";
import OvhProviderIntegration from "@/components/ovh-provider-integration";

interface ConnectorDialogsProps {
  connectorId: string;
  showGitHubDialog: boolean;
  showGcpDialog: boolean;
  showOvhDialog: boolean;
  onGitHubDialogChange: (open: boolean) => void;
  onGcpDialogChange: (open: boolean) => void;
  onOvhDialogChange: (open: boolean) => void;
  onGitHubDialogClose: () => void;
}

export function ConnectorDialogs({
  connectorId,
  showGitHubDialog,
  showGcpDialog,
  showOvhDialog,
  onGitHubDialogChange,
  onGcpDialogChange,
  onOvhDialogChange,
  onGitHubDialogClose,
}: ConnectorDialogsProps) {
  return (
    <>
      {connectorId === "github" && (
        <Dialog open={showGitHubDialog} onOpenChange={onGitHubDialogChange}>
          <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>GitHub Integration</DialogTitle>
            </DialogHeader>
            <GitHubProviderIntegration />
          </DialogContent>
        </Dialog>
      )}

      {connectorId === "gcp" && (
        <Dialog open={showGcpDialog} onOpenChange={onGcpDialogChange}>
          <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>GCP Project Management</DialogTitle>
            </DialogHeader>
            <GcpProviderIntegration onDisconnect={() => onGcpDialogChange(false)} />
          </DialogContent>
        </Dialog>
      )}

      {connectorId === "ovh" && (
        <Dialog open={showOvhDialog} onOpenChange={onOvhDialogChange}>
          <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>OVH Cloud Project Management</DialogTitle>
            </DialogHeader>
            <OvhProviderIntegration onDisconnect={() => onOvhDialogChange(false)} />
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

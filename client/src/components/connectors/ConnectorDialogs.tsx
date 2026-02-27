import React from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import GitHubProviderIntegration from "@/components/github-provider-integration";
import BitbucketProviderIntegration from "@/components/bitbucket-provider-integration";
import GcpProviderIntegration from "@/components/gcp-provider-integration";
import OvhProviderIntegration from "@/components/ovh-provider-integration";
import ScalewayProviderIntegration from "@/components/scaleway-provider-integration";
import AzureProviderIntegration from "@/components/azure-provider-integration";

interface ConnectorDialogsProps {
  connectorId: string;
  showGitHubDialog: boolean;
  showBitbucketDialog: boolean;
  showGcpDialog: boolean;
  showAzureDialog: boolean;
  showOvhDialog: boolean;
  showScalewayDialog: boolean;
  onGitHubDialogChange: (open: boolean) => void;
  onBitbucketDialogChange: (open: boolean) => void;
  onGcpDialogChange: (open: boolean) => void;
  onAzureDialogChange: (open: boolean) => void;
  onOvhDialogChange: (open: boolean) => void;
  onScalewayDialogChange: (open: boolean) => void;
  onGitHubDialogClose: () => void;
}

export function ConnectorDialogs({
  connectorId,
  showGitHubDialog,
  showBitbucketDialog,
  showGcpDialog,
  showAzureDialog,
  showOvhDialog,
  showScalewayDialog,
  onGitHubDialogChange,
  onBitbucketDialogChange,
  onGcpDialogChange,
  onAzureDialogChange,
  onOvhDialogChange,
  onScalewayDialogChange,
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

      {connectorId === "bitbucket" && (
        <Dialog open={showBitbucketDialog} onOpenChange={onBitbucketDialogChange}>
          <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Bitbucket Integration</DialogTitle>
            </DialogHeader>
            <BitbucketProviderIntegration />
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

      {connectorId === "azure" && (
        <Dialog open={showAzureDialog} onOpenChange={onAzureDialogChange}>
          <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Azure Subscription Management</DialogTitle>
            </DialogHeader>
            <AzureProviderIntegration onDisconnect={() => onAzureDialogChange(false)} />
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

      {connectorId === "scaleway" && (
        <Dialog open={showScalewayDialog} onOpenChange={onScalewayDialogChange}>
          <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Scaleway Project Management</DialogTitle>
            </DialogHeader>
            <ScalewayProviderIntegration onDisconnect={() => onScalewayDialogChange(false)} />
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

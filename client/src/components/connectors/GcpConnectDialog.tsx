"use client";

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Loader2 } from "lucide-react";
import { GcpServiceAccountForm } from "./GcpServiceAccountForm";

interface GcpConnectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Called after a successful service-account connect. The parent should
   * refresh connector status and close the dialog.
   */
  onSuccess: () => void;
  /**
   * Invoked when the user clicks "Connect with Google" in the OAuth tab.
   * This is the existing `handleGCPOAuth` from use-connector-oauth, which
   * redirects to Google and never returns, so the dialog does not need to
   * close on its own.
   */
  onOAuthConnect: () => Promise<void> | void;
}

export function GcpConnectDialog({
  open,
  onOpenChange,
  onSuccess,
  onOAuthConnect,
}: GcpConnectDialogProps) {
  const [oauthLoading, setOauthLoading] = useState(false);

  const handleOAuthClick = async () => {
    if (oauthLoading) return;
    setOauthLoading(true);
    try {
      await onOAuthConnect();
    } finally {
      // handleGCPOAuth navigates away on success, so in practice we only
      // reach here on an error — the hook already surfaces a toast, we
      // just need to re-enable the button.
      setOauthLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Connect Google Cloud</DialogTitle>
          <DialogDescription>
            Choose how Aurora should authenticate with your Google Cloud
            projects. You can always switch later.
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="oauth" className="w-full">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="oauth">OAuth</TabsTrigger>
            <TabsTrigger value="service-account">Service Account</TabsTrigger>
          </TabsList>

          <TabsContent value="oauth" className="space-y-4 pt-4">
            <div className="space-y-2 text-sm text-muted-foreground">
              <p>
                Sign in with your Google account. Aurora will use your OAuth
                consent to access the projects you select. Best for individual
                users and quick setup.
              </p>
              <p className="text-xs">
                You&apos;ll be redirected to Google to grant consent.
              </p>
            </div>
            <Button
              type="button"
              onClick={handleOAuthClick}
              disabled={oauthLoading}
              className="w-full h-10"
            >
              {oauthLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Redirecting...
                </>
              ) : (
                "Connect with Google"
              )}
            </Button>
          </TabsContent>

          <TabsContent value="service-account" className="pt-4">
            <div className="mb-4 space-y-1 text-sm text-muted-foreground">
              <p>
                Upload a GCP service account key (JSON). Best for teams,
                production environments, and CI/CD — no interactive sign-in
                required.
              </p>
            </div>
            <GcpServiceAccountForm onSuccess={onSuccess} />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

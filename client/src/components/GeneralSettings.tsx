"use client";

import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { Trash2, FileText } from "lucide-react";
import { clearTerraformState } from "@/lib/services/userSettings";
import { RCASettings } from "@/components/RCASettings";
import Link from "next/link";

export function GeneralSettings() {
  const [isClearingTerraformState, setIsClearingTerraformState] = useState(false);
  const { toast } = useToast();

  const handleClearTerraformState = async () => {
    setIsClearingTerraformState(true);
    try {
      const result = await clearTerraformState();
      
      if (result.success) {
        toast({
          title: "Success",
          description: result.message,
          variant: "default",
        });
      } else {
        toast({
          title: "Error",
          description: result.error || "Failed to clear Terraform state",
          variant: "destructive",
        });
      }
    } catch (error) {
      console.error("Error clearing Terraform state:", error);
      toast({
        title: "Error",
        description: "Failed to clear Terraform state",
        variant: "destructive",
      });
    } finally {
      setIsClearingTerraformState(false);
    }
  };

  const handleSyncInfrastructure = async () => {
    if (!userId) {
      toast({
        title: "Error",
        description: "You must be logged in to sync infrastructure",
        variant: "destructive",
      });
      return;
    }

    setIsSyncing(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL}/api/gcp/cloud-graph/sync`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          user_id: userId,
          provider: 'gcp'
        }),
      });

      const result = await response.json();

      if (response.ok) {
        toast({
          title: "Sync Started",
          description: result.message || "Infrastructure sync started.",
          variant: "default",
        });
        
        if (result.task_id) {
          pollTaskForFailures(result.task_id);
        }
      } else {
        toast({
          title: "Error",
          description: result.error || "Failed to trigger infrastructure sync",
          variant: "destructive",
        });
      }
    } catch (error) {
      console.error("Error syncing infrastructure:", error);
      toast({
        title: "Error",
        description: "Failed to trigger infrastructure sync",
        variant: "destructive",
      });
    } finally {
      setIsSyncing(false);
    }
  };

  const handleDisconnectCloudGraph = async () => {
    if (!userId) {
      toast({
        title: "Error",
        description: "You must be logged in",
        variant: "destructive",
      });
      return;
    }

    setIsDisconnectingCloudGraph(true);
    try {
      // Delete graph and feeds via backend API
      // Note: Cloud graph data can also be deleted from:
      // - AWS onboarding page (client/src/app/aws/onboarding/page.tsx:463) via cleanup endpoint
      const graphRes = await fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL}/api/gcp/cloud-graph/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, provider: 'gcp' }),
      });

      if (graphRes.ok) {
        toast({ title: "Success", description: "Cloud graph disconnected successfully" });
      } else {
        const error = await graphRes.json();
        throw new Error(error.error || 'Failed to disconnect');
      }
    } catch (error) {
      console.error("Error disconnecting cloud graph:", error);
      toast({ title: "Error", description: "Failed to disconnect cloud graph", variant: "destructive" });
    } finally {
      setIsDisconnectingCloudGraph(false);
    }
  };

  return (
    <div className="space-y-6 min-h-0">
      {/* System Settings Section */}
      <div>
        <Card>
          <CardHeader>
            <CardTitle>System Settings</CardTitle>
            <CardDescription>
              Manage your system preferences and data
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between p-4 border rounded-lg">
              <div className="space-y-1">
                <h4 className="font-medium">Clear Terraform State</h4>
                <p className="text-sm text-muted-foreground">
                  Remove all local Terraform state files. This helps resolve state conflicts when switching cloud providers.
                </p>
              </div>
              <Button
                variant="outline"
                onClick={handleClearTerraformState}
                disabled={isClearingTerraformState}
                className="text-orange-600 border-orange-200 hover:bg-orange-50 hover:text-orange-700"
              >
                <Trash2 className={`h-4 w-4 mr-2${isClearingTerraformState ? ' animate-pulse' : ''}`} />
                {isClearingTerraformState ? 'Clearing...' : 'Clear State'}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* RCA Notification Settings */}
      <RCASettings />

      {/* Legal & Privacy Section */}
      <div>
        <Card>
          <CardHeader>
            <CardTitle>Legal</CardTitle>
            <CardDescription>
              View our terms of service
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between p-4 border rounded-lg">
              <div className="space-y-1">
                <h4 className="font-medium">Terms of Service</h4>
                <p className="text-sm text-muted-foreground">
                  Read our terms of service and usage agreement
                </p>
              </div>
              <Link href="/terms" target="_blank">
                <Button variant="outline">
                  <FileText className="h-4 w-4 mr-2" />
                  View Terms
                </Button>
              </Link>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

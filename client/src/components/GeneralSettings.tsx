"use client";

import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { Trash2, FileText } from "lucide-react";
import { clearTerraformState } from "@/lib/services/userSettings";
import { RCASettings } from "@/components/RCASettings";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite } from "@/lib/roles";
import Link from "next/link";

export function GeneralSettings() {
  const [isClearingTerraformState, setIsClearingTerraformState] = useState(false);
  const { toast } = useToast();
  const { user } = useUser();
  const hasWriteAccess = canWrite(user?.role);

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
            {hasWriteAccess ? (
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
            ) : (
            <p className="text-sm text-muted-foreground p-4 border rounded-lg">
              Terraform state management requires Editor or Admin role.
            </p>
            )}
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

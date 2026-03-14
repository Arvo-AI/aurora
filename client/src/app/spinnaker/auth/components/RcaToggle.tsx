"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Zap } from "lucide-react";
import type { SpinnakerRcaSettings } from "@/lib/services/spinnaker";

interface RcaToggleProps {
  rcaSettings: SpinnakerRcaSettings | null;
  loading: boolean;
  onToggle: (enabled: boolean) => void;
}

export function RcaToggle({ rcaSettings, loading, onToggle }: RcaToggleProps) {
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="flex items-center justify-between">
          <div className="space-y-1 flex-1">
            <h4 className="font-medium flex items-center gap-2">
              <Zap className="h-4 w-4" />
              Automatic RCA on Deployment Failures
            </h4>
            <p className="text-sm text-muted-foreground">
              Automatically trigger root cause analysis when a Spinnaker pipeline fails
            </p>
          </div>
          <Switch
            checked={rcaSettings?.rcaEnabled ?? true}
            onCheckedChange={onToggle}
            disabled={loading}
            className="ml-4"
          />
        </div>
      </CardContent>
    </Card>
  );
}

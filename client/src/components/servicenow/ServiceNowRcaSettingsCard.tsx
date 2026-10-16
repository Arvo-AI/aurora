"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import {
  serviceNowService,
  SERVICENOW_PRIORITY_OPTIONS,
  type ServiceNowMinPriority,
  type ServiceNowRcaSettings,
} from "@/lib/services/servicenow";

const DEFAULT_SETTINGS: ServiceNowRcaSettings = {
  rcaEnabled: true,
  minPriority: 3,
  workNotesEnabled: true,
};

export function ServiceNowRcaSettingsCard() {
  const { toast } = useToast();
  const [settings, setSettings] = useState<ServiceNowRcaSettings>(DEFAULT_SETTINGS);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const result = await serviceNowService.getRcaSettings();
      if (cancelled) return;
      if (result) setSettings(result);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const update = async (patch: Partial<ServiceNowRcaSettings>) => {
    setSaving(true);
    const result = await serviceNowService.updateRcaSettings(patch);
    if (result) {
      setSettings(result);
      toast({ title: "Settings updated", description: "ServiceNow RCA settings saved." });
    } else {
      toast({
        title: "Failed to update settings",
        description: "Could not update RCA settings. Please try again.",
        variant: "destructive",
      });
    }
    setSaving(false);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Root Cause Analysis</CardTitle>
        <CardDescription>Choose which ServiceNow incidents Aurora investigates and where results go.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {loading ? (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading settings...
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between gap-4">
              <div className="space-y-0.5">
                <Label htmlFor="servicenow-rca-enabled" className="text-sm font-medium">
                  Investigate new incidents
                </Label>
                <p className="text-sm text-muted-foreground">
                  Automatically run an investigation when ServiceNow reports a new incident.
                </p>
              </div>
              <Switch
                id="servicenow-rca-enabled"
                checked={settings.rcaEnabled}
                onCheckedChange={(checked) => update({ rcaEnabled: checked })}
                disabled={saving}
              />
            </div>

            {settings.rcaEnabled && (
              <div className="flex items-center justify-between gap-4">
                <div className="space-y-0.5">
                  <Label htmlFor="servicenow-min-priority" className="text-sm font-medium">
                    Minimum priority
                  </Label>
                  <p className="text-sm text-muted-foreground">
                    Investigate incidents at this priority or more urgent.
                  </p>
                </div>
                <Select
                  value={String(settings.minPriority)}
                  onValueChange={(v) => update({ minPriority: Number(v) as ServiceNowMinPriority })}
                  disabled={saving}
                >
                  <SelectTrigger id="servicenow-min-priority" className="w-56">
                    <SelectValue placeholder="Select priority" />
                  </SelectTrigger>
                  <SelectContent>
                    {SERVICENOW_PRIORITY_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={String(opt.value)}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            <div className="flex items-center justify-between gap-4">
              <div className="space-y-0.5">
                <Label htmlFor="servicenow-work-notes" className="text-sm font-medium">
                  Post the RCA as a work note on the ServiceNow incident
                </Label>
                <p className="text-sm text-muted-foreground">
                  Writes the root cause summary back to the incident when the investigation finishes.
                </p>
              </div>
              <Switch
                id="servicenow-work-notes"
                checked={settings.workNotesEnabled}
                onCheckedChange={(checked) => update({ workNotesEnabled: checked })}
                disabled={saving}
              />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

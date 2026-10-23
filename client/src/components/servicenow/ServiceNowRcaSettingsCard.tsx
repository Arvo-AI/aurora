"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite as checkCanWrite } from "@/lib/roles";
import { PolicyMemoryCard } from "@/components/PolicyMemoryCard";
import { ServiceNowAutomaticRca } from "@/components/servicenow/ServiceNowAutomaticRca";
import {
  serviceNowService,
  type ServiceNowRcaMode,
  type ServiceNowRcaRule,
  type ServiceNowRcaSettings,
  type ServiceNowWriteMode,
} from "@/lib/services/servicenow";

const BRAND = "#62D84E";

export function ServiceNowRcaSettingsCard() {
  const { toast } = useToast();
  const { user } = useUser();
  const canWrite = checkCanWrite(user?.role);

  const [settings, setSettings] = useState<ServiceNowRcaSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [closeCodeDraft, setCloseCodeDraft] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadFailed(false);
    try {
      const result = await serviceNowService.getRcaSettings();
      setSettings(result);
      setCloseCodeDraft(result.resolveCloseCode);
    } catch {
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Optimistic save: apply, PUT, and roll back on failure. Returns what the server stored.
  const save = async (
    patch: Partial<ServiceNowRcaSettings>,
    description: string,
  ): Promise<ServiceNowRcaSettings | null> => {
    const previous = settings;
    if (!previous) return null;
    setSettings({ ...previous, ...patch });
    setSaving(true);
    try {
      const stored = await serviceNowService.updateRcaSettings(patch);
      setSettings(stored);
      setCloseCodeDraft(stored.resolveCloseCode);
      toast({ title: "Settings saved", description });
      return stored;
    } catch (err) {
      setSettings(previous);
      toast({
        title: "Failed to save settings",
        description: err instanceof Error && err.message ? err.message : undefined,
        variant: "destructive",
      });
      return null;
    } finally {
      setSaving(false);
    }
  };

  const saveRcaMode = (mode: ServiceNowRcaMode) => save(
    { rcaMode: mode },
    mode === "auto" ? "Aurora decides which incidents to investigate" : "Automatic investigations follow your filters",
  );

  const saveRules = async (rules: ServiceNowRcaRule[]) => {
    const stored = await save(
      { rcaRules: rules },
      rules.length === 0 ? "Automatic ServiceNow investigations are off" : "Automatic investigations follow the filters you saved",
    );
    return stored !== null;
  };

  const saveWorkNotes = (enabled: boolean) => save(
    { workNotesEnabled: enabled },
    enabled ? "Aurora will post work notes on ServiceNow incidents" : "Aurora will not write to ServiceNow",
  );

  const saveWriteMode = (mode: ServiceNowWriteMode) => save(
    { writeMode: mode },
    mode === "full" ? "Aurora can create and update incidents" : "Aurora will only post work notes",
  );

  const saveResolveSync = (enabled: boolean) => save(
    { resolveSync: enabled },
    enabled ? "Resolving an incident in Aurora resolves it in ServiceNow" : "Aurora will not resolve ServiceNow incidents",
  );

  const saveCloseCode = () => {
    const code = closeCodeDraft.trim();
    if (!settings || !code || code === settings.resolveCloseCode) return;
    void save({ resolveCloseCode: code }, `Close code set to "${code}"`);
  };

  const disabled = saving || !canWrite;

  const renderBody = () => {
    if (loading) {
      return (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading settings...
        </div>
      );
    }
    // A failed read must not render editable empty settings.
    if (loadFailed || !settings) {
      return (
        <div className="space-y-2">
          <p className="text-sm text-destructive">Couldn&apos;t load the ServiceNow RCA settings.</p>
          <Button type="button" variant="outline" size="sm" className="h-8" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      );
    }

    return (
      <>
        <section className="space-y-3">
          <div>
            <p className="text-sm font-medium">Automatic RCA</p>
            <p className="text-sm text-muted-foreground">
              Which new ServiceNow incidents Aurora investigates on its own.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Automatic RCA mode">
            {(["auto", "filters"] as const).map((value) => {
              const selected = settings.rcaMode === value;
              return (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => { if (!selected) void saveRcaMode(value); }}
                  disabled={disabled}
                  className={`rounded-lg border p-2.5 text-left transition-colors ${
                    selected ? "border-[#62D84E] bg-[#62D84E]/[0.06]" : "border-border hover:bg-muted/50"
                  }`}
                >
                  <span className="text-sm font-medium">{value === "auto" ? "Auto" : "Filters"}</span>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {value === "auto"
                      ? "Aurora judges each incident using the memory below"
                      : "Only incidents matching your filters"}
                  </p>
                </button>
              );
            })}
          </div>
          {settings.rcaMode === "auto" ? (
            <PolicyMemoryCard
              canWrite={canWrite}
              endpoint="servicenow"
              name="ServiceNow RCA"
              className="shadow-none"
              placeholder="Describe which ServiceNow incidents Aurora should investigate (markdown)."
              description={
                <>
                  Aurora reads this before judging each new incident. It also updates it when your
                  team says what to investigate, e.g. &quot;never investigate Service Desk incidents&quot;.
                  Filters are ignored in Auto. Updates to incidents Aurora already tracks only sync their state.
                </>
              }
            />
          ) : (
            <ServiceNowAutomaticRca rules={settings.rcaRules} saving={saving} onSave={saveRules} />
          )}
        </section>

        <section className="space-y-4 border-t pt-5">
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-0.5">
              <Label htmlFor="servicenow-work-notes" className="text-sm font-medium">
                Post work notes on ServiceNow incidents
              </Label>
              <p className="text-sm text-muted-foreground">
                Off by default. When on, Aurora posts the root cause as a work note when an investigation
                finishes, and the agent may post work notes during unattended investigations.
              </p>
            </div>
            <Switch
              id="servicenow-work-notes"
              checked={settings.workNotesEnabled}
              onCheckedChange={(checked) => void saveWorkNotes(checked)}
              disabled={disabled}
            />
          </div>

          {settings.workNotesEnabled && (
            <div className="space-y-3 pl-1">
              <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="ServiceNow write scope">
                {(["notes_only", "full"] as const).map((value) => {
                  const selected = settings.writeMode === value;
                  return (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={selected}
                      onClick={() => { if (!selected) void saveWriteMode(value); }}
                      disabled={disabled}
                      className={`rounded-lg border p-2.5 text-left transition-colors ${
                        selected ? "border-[#62D84E] bg-[#62D84E]/[0.06]" : "border-border hover:bg-muted/50"
                      }`}
                    >
                      <span className="text-sm font-medium">
                        {value === "full" ? "Create & update incidents" : "Work notes only"}
                      </span>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        {value === "full"
                          ? "Aurora may also create incidents and change state, priority and assignment"
                          : "Aurora only adds work notes to existing incidents"}
                      </p>
                    </button>
                  );
                })}
              </div>

              {settings.writeMode === "full" && (
                <div className="space-y-3 rounded-md border p-2.5">
                  <div className="flex items-center justify-between gap-4">
                    <div className="space-y-0.5">
                      <Label htmlFor="servicenow-resolve-sync" className="text-sm font-medium">
                        Resolve the ServiceNow incident when it&apos;s resolved in Aurora
                      </Label>
                      <p className="text-sm text-muted-foreground">
                        Sets the state to Resolved with the close code below and the root cause as close notes.
                      </p>
                    </div>
                    <Switch
                      id="servicenow-resolve-sync"
                      checked={settings.resolveSync}
                      onCheckedChange={(checked) => void saveResolveSync(checked)}
                      disabled={disabled}
                    />
                  </div>
                  {settings.resolveSync && (
                    <div className="space-y-1.5">
                      <Label htmlFor="servicenow-close-code" className="text-xs">Close code</Label>
                      <Input
                        id="servicenow-close-code"
                        value={closeCodeDraft}
                        onChange={(event) => setCloseCodeDraft(event.target.value)}
                        onBlur={saveCloseCode}
                        onKeyDown={(event) => { if (event.key === "Enter") (event.target as HTMLInputElement).blur(); }}
                        placeholder="Solution provided"
                        className="h-8 max-w-xs text-sm"
                        disabled={disabled}
                      />
                      <p className="text-xs text-muted-foreground">
                        Must be one of the resolution codes on your instance.
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </section>
      </>
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Root Cause Analysis</CardTitle>
        <CardDescription>Choose which ServiceNow incidents Aurora investigates and what it may write back.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">{renderBody()}</CardContent>
    </Card>
  );
}

export { BRAND as SERVICENOW_BRAND_COLOR };

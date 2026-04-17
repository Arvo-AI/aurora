"use client";

import React, { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Loader2,
  Save,
  MessageSquare,
  AlertCircle,
  RotateCcw,
  Check,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import {
  googleChatService,
  type BotSpace,
  type TeamMapping,
} from "@/lib/services/google-chat";

interface MergedSpace {
  spaceName: string;
  displayName: string;
  googleDescription: string;
  teamName: string;
  description: string;
  hasOverride: boolean;
  mappingId: number | null;
  dirty: boolean;
}

export default function GoogleChatTeamMappings() {
  const { toast } = useToast();
  const [spaces, setSpaces] = useState<MergedSpace[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [savingSpace, setSavingSpace] = useState<string | null>(null);

  const [routingInstructions, setRoutingInstructions] = useState("");
  const [savedInstructions, setSavedInstructions] = useState("");
  const [isSavingInstructions, setIsSavingInstructions] = useState(false);

  const instructionsChanged = routingInstructions !== savedInstructions;

  const loadData = useCallback(async () => {
    setIsLoading(true);
    try {
      const [botSpaces, mappings, instructions] = await Promise.all([
        googleChatService.listBotSpaces().catch(() => [] as BotSpace[]),
        googleChatService.getTeamMappings().catch(() => [] as TeamMapping[]),
        googleChatService.getRoutingInstructions().catch(() => ""),
      ]);

      const overrideMap = new Map<string, TeamMapping>();
      for (const m of mappings) {
        overrideMap.set(m.space_name, m);
      }

      const merged: MergedSpace[] = botSpaces.map((bs) => {
        const override = overrideMap.get(bs.name);
        return {
          spaceName: bs.name,
          displayName: bs.displayName,
          googleDescription: bs.description || "",
          teamName: override?.team_name || bs.displayName,
          description: override?.description || bs.description || "",
          hasOverride: !!override,
          mappingId: override?.id ?? null,
          dirty: false,
        };
      });

      setSpaces(merged);
      setRoutingInstructions(instructions);
      setSavedInstructions(instructions);
    } catch (error: any) {
      toast({
        title: "Failed to load",
        description: error.message || "Could not load spaces",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const updateField = (
    spaceName: string,
    field: "teamName" | "description",
    value: string
  ) => {
    setSpaces((prev) =>
      prev.map((s) =>
        s.spaceName === spaceName ? { ...s, [field]: value, dirty: true } : s
      )
    );
  };

  const resetToDefaults = async (spaceName: string) => {
    const space = spaces.find((s) => s.spaceName === spaceName);
    if (!space) return;

    if (space.hasOverride && space.mappingId) {
      try {
        await googleChatService.deleteTeamMapping(space.mappingId);
      } catch {
        // best-effort — still reset the UI
      }
    }

    setSpaces((prev) =>
      prev.map((s) =>
        s.spaceName === spaceName
          ? {
              ...s,
              teamName: s.displayName,
              description: s.googleDescription,
              hasOverride: false,
              mappingId: null,
              dirty: false,
            }
          : s
      )
    );
  };

  const handleSaveSpace = async (space: MergedSpace) => {
    setSavingSpace(space.spaceName);
    try {
      const result = await googleChatService.upsertTeamMapping({
        team_name: space.teamName.trim(),
        space_name: space.spaceName,
        space_display_name: space.displayName,
        description: space.description.trim() || undefined,
      });
      setSpaces((prev) =>
        prev.map((s) =>
          s.spaceName === space.spaceName
            ? { ...s, dirty: false, hasOverride: true, mappingId: result.id }
            : s
        )
      );
      toast({ title: `Saved ${space.teamName}` });
    } catch (error: any) {
      toast({
        title: "Save failed",
        description: error.message || "Could not save",
        variant: "destructive",
      });
    } finally {
      setSavingSpace(null);
    }
  };

  const handleSaveInstructions = async () => {
    setIsSavingInstructions(true);
    try {
      await googleChatService.updateRoutingInstructions(routingInstructions);
      setSavedInstructions(routingInstructions);
      toast({ title: "Routing instructions saved" });
    } catch (error: any) {
      toast({
        title: "Save failed",
        description: error.message || "Could not save routing instructions",
        variant: "destructive",
      });
    } finally {
      setIsSavingInstructions(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Info banner */}
      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3">
        <p className="text-sm text-muted-foreground">
          Aurora auto-detects all Google Chat spaces the bot is in. The team
          names and descriptions below are what the AI sees when routing
          incidents. Edit them to help it route more accurately, or write
          routing instructions for custom rules.
        </p>
      </div>

      {/* Routing instructions */}
      <div className="space-y-2">
        <h4 className="text-sm font-medium">Routing Instructions</h4>
        <p className="text-xs text-muted-foreground">
          Free-form rules that tell Aurora how to route incidents. Be as
          specific as you like.
        </p>
        <Textarea
          placeholder={`Examples:\n- Always include DevOps on every incident\n- If the issue is related to payments or billing, page Finance and Payments\n- P1 incidents should go to all teams\n- Database issues go to the Platform team`}
          value={routingInstructions}
          onChange={(e) => setRoutingInstructions(e.target.value)}
          rows={4}
          className="resize-y text-sm"
        />
        <Button
          onClick={handleSaveInstructions}
          disabled={!instructionsChanged || isSavingInstructions}
          size="sm"
          variant={instructionsChanged ? "default" : "outline"}
        >
          {isSavingInstructions ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <Save className="h-4 w-4 mr-2" />
          )}
          Save Instructions
        </Button>
      </div>

      {/* Detected spaces */}
      <div className="space-y-3">
        <h4 className="text-sm font-medium">
          Teams{" "}
          <span className="text-muted-foreground font-normal">
            ({spaces.length} space{spaces.length !== 1 ? "s" : ""} detected)
          </span>
        </h4>

        {spaces.length === 0 ? (
          <div className="flex items-start gap-2 rounded-lg border border-yellow-500/20 bg-yellow-500/5 p-3">
            <AlertCircle className="h-4 w-4 shrink-0 text-yellow-500 mt-0.5" />
            <p className="text-sm text-muted-foreground">
              The Aurora Chat app isn&apos;t a member of any Google Chat spaces
              yet. Add the app to your team spaces first, then they&apos;ll
              appear here automatically.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {spaces.map((s) => (
              <div
                key={s.spaceName}
                className="rounded-lg border px-3 py-3 space-y-2"
              >
                <div className="flex items-center gap-2">
                  <MessageSquare className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="text-xs text-muted-foreground truncate">
                    {s.displayName}
                  </span>
                  {s.hasOverride && !s.dirty && (
                    <Check className="h-3 w-3 text-green-500 shrink-0" />
                  )}
                </div>
                <div className="grid grid-cols-[1fr_auto] gap-2">
                  <div className="space-y-1.5">
                    <Input
                      value={s.teamName}
                      onChange={(e) =>
                        updateField(s.spaceName, "teamName", e.target.value)
                      }
                      placeholder="Team name"
                      className="text-sm h-8"
                    />
                    <Input
                      value={s.description}
                      onChange={(e) =>
                        updateField(s.spaceName, "description", e.target.value)
                      }
                      placeholder="Description — what does this team handle?"
                      className="text-sm h-8 text-muted-foreground"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Button
                      size="icon"
                      variant={s.dirty ? "default" : "outline"}
                      className="h-8 w-8"
                      disabled={
                        !s.teamName.trim() || savingSpace === s.spaceName
                      }
                      onClick={() => handleSaveSpace(s)}
                      title="Save override"
                    >
                      {savingSpace === s.spaceName ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Save className="h-3.5 w-3.5" />
                      )}
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-8 w-8"
                      onClick={() => resetToDefaults(s.spaceName)}
                      title="Reset to Google defaults"
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

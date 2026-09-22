"use client";

import { useCallback, useEffect, useState } from "react";
import { MessageSquareText } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { NotificationToggle } from "@/components/NotificationToggle";
import { useToast } from "@/hooks/use-toast";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite as checkCanWrite } from "@/lib/roles";
import { pagerdutyService, type PagerDutyStatus } from "@/lib/services/pagerduty";

const PREFERENCE_KEY = "pagerduty_incident_notes";

const TOGGLE_DESCRIPTION =
  "Post RCA notes to PagerDuty incidents. Aurora adds one note per incident when an investigation completes. " +
  "Notes cannot be edited or deleted once posted, and require a PagerDuty user token with write access.";

// Fallback for a cached status saved before the server sent notesUnwritableReason.
const UNWRITABLE_FALLBACK = "This token cannot post notes. Rotate to a user API token from a user with write access.";

interface PagerDutyNotesCardProps {
  status: PagerDutyStatus;
}

export function PagerDutyNotesCard({ status }: Readonly<PagerDutyNotesCardProps>) {
  const { toast } = useToast();
  const { user } = useUser();
  const canWrite = checkCanWrite(user?.role);
  // Legacy cached status may lack the field entirely; treat anything but true as unwritable.
  const tokenCanWrite = status.capabilities?.can_write_incidents === true;

  const [enabled, setEnabled] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const loadPreference = useCallback(async () => {
    try {
      const params = new URLSearchParams({ keys: PREFERENCE_KEY });
      const response = await fetch(`/api/proxy/user-preferences/batch?${params.toString()}`);
      if (response.ok) {
        const data = await response.json();
        const value = data.preferences?.[PREFERENCE_KEY];
        setEnabled(value === true || value === "true");
      }
    } catch (error) {
      console.error("[pagerduty] Failed to load notes preference", error);
    } finally {
      setIsLoading(false);
    }
  }, []);

  // Re-read after connect/rotate too: the server turns the toggle off when the
  // new credentials cannot write (status.notesDisabled), so mount-only is stale.
  useEffect(() => {
    loadPreference();
  }, [loadPreference, status.validatedAt, tokenCanWrite]);

  const handleChange = async (next: boolean) => {
    setEnabled(next);
    setIsSaving(true);
    try {
      if (next) {
        await pagerdutyService.enableNotes();
      } else {
        const response = await fetch("/api/proxy/user-preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key: PREFERENCE_KEY, value: false }),
        });
        if (!response.ok) {
          throw new Error("Failed to turn off PagerDuty notes");
        }
      }
    } catch (error: unknown) {
      setEnabled(!next);
      toast({
        variant: "destructive",
        description: error instanceof Error && error.message ? error.message : "Failed to update PagerDuty notes",
      });
    } finally {
      setIsSaving(false);
    }
  };

  const description = tokenCanWrite
    ? TOGGLE_DESCRIPTION
    : `${TOGGLE_DESCRIPTION} ${status.notesUnwritableReason ?? UNWRITABLE_FALLBACK}`;

  return (
    <Card>
      <CardHeader>
        <CardTitle>RCA Notes</CardTitle>
        <CardDescription>
          Write Aurora&apos;s root cause back onto the PagerDuty incident that triggered the investigation.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <NotificationToggle
          title="Post RCA notes to PagerDuty"
          description={description}
          icon={<MessageSquareText className="h-4 w-4" />}
          checked={enabled}
          onChange={handleChange}
          isLoading={isLoading || isSaving}
          // Turning off is always allowed; only turning on needs a writable token
          disabled={!canWrite || (!tokenCanWrite && !enabled)}
        />
      </CardContent>
    </Card>
  );
}

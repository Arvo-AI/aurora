"use client";

import { useState } from "react";
import { MessageSquareText } from "lucide-react";
import { NotificationToggle } from "@/components/NotificationToggle";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/hooks/useAuthHooks";
import { fetchR, jsonFetcher, useQuery } from "@/lib/query";
import { canWrite } from "@/lib/roles";

type Source = "cloudwatch" | "sentry" | "pagerduty" | "incidentio";
const endpoint = "/api/proxy/user-preferences";

export function AlertThreadReplyToggle({ source }: { source: Source }) {
  const { orgId, userId, role } = useAuth();
  const { toast } = useToast();
  const [saving, setSaving] = useState(false);
  const preference = `slack_${source}_rca_replies`;
  const { data, error, mutate } = useQuery(
    userId ? `alert-thread-replies/${orgId ?? userId}/${source}` : null,
    (_, signal) => jsonFetcher<{ value: boolean | null }>(`${endpoint}?key=${preference}`, signal),
  );

  async function save(enabled: boolean) {
    setSaving(true);
    try {
      const response = await fetchR(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: preference, value: enabled }),
        retries: 0,
      });
      if (!response.ok) throw new Error("Save failed");
    } catch {
      toast({ variant: "destructive", description: "Couldn’t save the Slack reply setting." });
    } finally {
      await mutate();
      setSaving(false);
    }
  }

  if (error) {
    return (
      <p role="alert" className="text-sm text-muted-foreground">
        Couldn’t load the Slack reply setting.{" "}
        <button type="button" className="underline" onClick={() => void mutate()}>Retry</button>
      </p>
    );
  }
  if (!data) {
    return <p role="status" className="text-sm text-muted-foreground">Loading Slack reply setting…</p>;
  }
  return (
    <NotificationToggle
      title="Reply to the alert with the RCA"
      description="Aurora replies in the original Slack alert thread when the investigation finishes. Requires alert thread replies to be enabled in Slack settings."
      icon={<MessageSquareText className="h-4 w-4" aria-hidden="true" />}
      checked={data.value ?? true}
      onChange={save}
      isLoading={saving}
      disabled={!canWrite(role)}
    />
  );
}

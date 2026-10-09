"use client";

import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { TeamsStatus } from "@/lib/services/teams";

export function TeamsSetupIncompleteBanner({
  status,
  compact = false,
}: {
  status: TeamsStatus;
  compact?: boolean;
}) {
  if (!status.connected || status.setup_complete) {
    return null;
  }

  const steps = status.pending_setup?.length
    ? status.pending_setup
    : [
        {
          id: "install_teams_app",
          title: "Install Aurora in Microsoft Teams",
          detail: "Add the app per team and @mention Aurora in a channel.",
        },
      ];

  if (compact) {
    return (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
        <div className="flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5 text-amber-600 dark:text-amber-400" />
          <div>
            <p className="font-medium">Teams setup incomplete</p>
            <p className="text-muted-foreground mt-0.5">
              OAuth is done. {steps[0]?.title} —{" "}
              <Link href="/teams/setup" className="text-primary hover:underline">
                finish setup
              </Link>
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <Card className="mb-6 border-amber-500/40 bg-amber-500/[0.06]">
      <CardHeader className="pb-2">
        <CardTitle className="text-lg flex items-center gap-2 text-amber-800 dark:text-amber-200">
          <AlertTriangle className="h-5 w-5 text-amber-600 dark:text-amber-400" />
          Teams setup incomplete
        </CardTitle>
        <CardDescription>
          Microsoft sign-in (OAuth) succeeded. Aurora still needs the Teams app installed where you
          want @mentions and bot messages — OAuth cannot do that step.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm font-medium text-foreground">What&apos;s left</p>
        <ol className="list-decimal list-inside space-y-2 text-sm text-muted-foreground">
          {steps.map((step) => (
            <li key={step.id}>
              <span className="font-medium text-foreground">{step.title}</span>
              <span className="block pl-5 text-xs mt-0.5">{step.detail}</span>
            </li>
          ))}
        </ol>
        <p className="text-xs text-muted-foreground">
          <Link href="/teams/setup" className="text-primary hover:underline">
            Full setup steps
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}

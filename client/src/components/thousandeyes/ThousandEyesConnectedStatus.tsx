"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { ThousandEyesStatus } from "@/lib/services/thousandeyes";

function formatConnectionDate(isoString: string | undefined): string {
  if (!isoString) return "just now";
  const date = new Date(isoString);
  return Number.isNaN(date.getTime()) ? "just now" : date.toLocaleString();
}

interface ThousandEyesConnectedStatusProps {
  status: ThousandEyesStatus;
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

export function ThousandEyesConnectedStatus({
  status,
  onDisconnect,
  loading,
}: ThousandEyesConnectedStatusProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>ThousandEyes Connected</CardTitle>
        <CardDescription>
          Aurora is connected to ThousandEyes and can access network intelligence data
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid md:grid-cols-2 gap-4">
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Account Group ID</p>
            <p className="text-base font-semibold truncate">
              {status.account_group_id || "Default"}
            </p>
          </div>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Account Groups</p>
            <p className="text-base font-semibold">
              {status.account_groups?.length ?? 0} available
            </p>
          </div>
        </div>

        {status.account_groups && status.account_groups.length > 0 && (
          <div className="space-y-2">
            <p className="text-sm font-medium">Available Account Groups</p>
            <div className="flex flex-wrap gap-2">
              {status.account_groups.map((group) => (
                <span
                  key={group.aid}
                  className="inline-flex items-center px-3 py-1 rounded-full bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 text-xs font-medium text-blue-800 dark:text-blue-300"
                >
                  {group.accountGroupName || group.aid}
                </span>
              ))}
            </div>
          </div>
        )}

        <div className="border rounded-lg p-4 bg-muted/40 space-y-2 text-sm text-muted-foreground">
          <p className="font-medium text-foreground">Available Data</p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            {[
              "Tests & Results",
              "Alerts & Rules",
              "Cloud/Enterprise Agents",
              "Endpoint Agents",
              "Dashboards & Widgets",
              "Path Visualization",
              "Network Outages",
              "App Outages",
              "BGP Monitors & Routes",
              "DNS & DNSSEC",
              "Page Load & Transactions",
              "VoIP & SIP",
            ].map((item) => (
              <div key={item} className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
                {item}
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="text-xs text-muted-foreground">
            Connected since{" "}
            <strong>{formatConnectionDate(status.validatedAt)}</strong>
          </div>
          <Button variant="outline" onClick={onDisconnect} disabled={loading}>
            {loading ? "Disconnecting\u2026" : "Disconnect ThousandEyes"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

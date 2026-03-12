"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ExternalLink } from "lucide-react";
import type { SpinnakerStatus } from "@/lib/services/spinnaker";

const toSafeExternalUrl = (value?: string): string | null => {
  if (!value) return null;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:" ? u.toString() : null;
  } catch {
    return null;
  }
};

interface ConnectionInfoProps {
  status: SpinnakerStatus;
}

export function ConnectionInfo({ status }: ConnectionInfoProps) {
  const safeUrl = toSafeExternalUrl(status.baseUrl);

  return (
    <Card>
      <CardContent className="pt-6 space-y-6">
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium truncate">{status.baseUrl}</p>
            <p className="text-xs text-muted-foreground mt-0.5">
              Auth: {status.authType || "Token/Basic"}
            </p>
          </div>
          {safeUrl && (
            <a href={safeUrl} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors shrink-0">
              Open Dashboard
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>

        <div className="border-t" />
        <div className="grid grid-cols-2 gap-4">
          <div className="text-center">
            <p className="text-2xl font-semibold tabular-nums">{status.applications ?? 0}</p>
            <p className="text-[11px] text-muted-foreground mt-0.5">Applications</p>
          </div>
          <div className="text-center">
            <p className="text-2xl font-semibold tabular-nums">{status.cloudAccounts?.length ?? 0}</p>
            <p className="text-[11px] text-muted-foreground mt-0.5">Cloud Accounts</p>
          </div>
        </div>

        {status.cloudAccounts && status.cloudAccounts.length > 0 && (
          <>
            <div className="border-t" />
            <div className="space-y-2">
              <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Cloud Accounts</p>
              <div className="flex flex-wrap gap-1.5">
                {status.cloudAccounts.map((account) => (
                  <Badge key={account} variant="secondary" className="text-xs">
                    {account}
                  </Badge>
                ))}
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

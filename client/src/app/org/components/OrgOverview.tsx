"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Copy, Check } from "lucide-react";
import type { OrgMember } from "../page";

interface OrgOverviewProps {
  org: {
    id: string;
    name: string;
    slug: string;
    createdBy: string;
    createdAt: string;
    members: OrgMember[];
  };
  isAdmin: boolean;
}

export default function OrgOverview({ org, isAdmin }: OrgOverviewProps) {
  const [stats, setStats] = useState<{
    members: number;
    incidents: number;
    chatSessions: number;
    integrations: number;
  } | null>(null);
  const [copied, setCopied] = useState(false);

  const signUpUrl =
    typeof window !== "undefined" ? `${window.location.origin}/sign-up` : "/sign-up";

  useEffect(() => {
    fetch("/api/orgs/stats")
      .then((r) => r.json())
      .then(setStats)
      .catch(() => {});
  }, []);

  function copyLink() {
    navigator.clipboard.writeText(signUpUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const roleCounts = org.members.reduce(
    (acc, m) => {
      acc[m.role || "viewer"] = (acc[m.role || "viewer"] || 0) + 1;
      return acc;
    },
    {} as Record<string, number>
  );

  return (
    <div className="space-y-10">
      {/* Compact stats — inline, not cards */}
      <div className="grid grid-cols-4 gap-px bg-border rounded-lg overflow-hidden">
        {[
          { label: "Members", value: stats?.members ?? org.members.length },
          { label: "Integrations", value: stats?.integrations ?? 0 },
          { label: "Incidents", value: stats?.incidents ?? 0 },
          { label: "Conversations", value: stats?.chatSessions ?? 0 },
        ].map((s) => (
          <div key={s.label} className="bg-background px-5 py-4">
            <p className="text-2xl font-semibold tabular-nums">{s.value}</p>
            <p className="text-xs text-muted-foreground mt-0.5">{s.label}</p>
          </div>
        ))}
      </div>

      {/* People strip — faces, not a table */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">People</h3>
          <span className="text-xs text-muted-foreground">
            {org.members.length} total
          </span>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          {/* Stacked avatars for each role group */}
          {(["admin", "editor", "viewer"] as const).map((role) => {
            const count = roleCounts[role] || 0;
            if (count === 0) return null;
            const members = org.members.filter((m) => (m.role || "viewer") === role);
            return (
              <div key={role} className="flex items-center gap-2 pr-4 border-r border-border last:border-0 last:pr-0">
                <div className="flex -space-x-2">
                  {members.slice(0, 3).map((m) => (
                    <div
                      key={m.id}
                      className="h-7 w-7 rounded-full bg-muted border-2 border-background flex items-center justify-center text-[10px] font-medium text-muted-foreground"
                      title={m.name || m.email}
                    >
                      {(m.name || m.email).charAt(0).toUpperCase()}
                    </div>
                  ))}
                  {count > 3 && (
                    <div className="h-7 w-7 rounded-full bg-muted border-2 border-background flex items-center justify-center text-[10px] font-medium text-muted-foreground">
                      +{count - 3}
                    </div>
                  )}
                </div>
                <span className="text-xs text-muted-foreground">
                  {count} {role}{count !== 1 ? "s" : ""}
                </span>
              </div>
            );
          })}
        </div>
      </section>

      {/* Invite block — understated */}
      <section className="rounded-lg border border-border p-5">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-medium">Invite your team</h3>
            <p className="text-[13px] text-muted-foreground leading-relaxed max-w-md">
              Anyone with this link can create an account and join your organization.
              New members start with read-only access{isAdmin ? " — promote them from the Members tab" : ""}.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 mt-4">
          <div className="flex-1 bg-muted/40 rounded-md border border-border/50 px-3 py-2">
            <code className="text-[13px] text-foreground/70 select-all break-all">{signUpUrl}</code>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={copyLink}
            className="gap-1.5 h-9 px-3 flex-shrink-0"
          >
            {copied ? (
              <>
                <Check className="h-3.5 w-3.5" />
                Copied
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5" />
                Copy
              </>
            )}
          </Button>
        </div>
      </section>

    </div>
  );
}

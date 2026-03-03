"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Users,
  Zap,
  MessageSquare,
  Plug,
  Copy,
  Check,
  Eye,
  Pencil,
  Crown,
} from "lucide-react";
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

const ROLE_CONFIG = {
  admin: { icon: Crown, color: "text-purple-500", bg: "bg-purple-500/10", label: "Admins" },
  editor: { icon: Pencil, color: "text-amber-500", bg: "bg-amber-500/10", label: "Editors" },
  viewer: { icon: Eye, color: "text-blue-500", bg: "bg-blue-500/10", label: "Viewers" },
} as const;

export default function OrgOverview({ org, isAdmin }: OrgOverviewProps) {
  const [stats, setStats] = useState<{
    members: number;
    incidents: number;
    chatSessions: number;
    integrations: number;
  } | null>(null);
  const [copied, setCopied] = useState(false);

  const signUpUrl =
    typeof window !== "undefined"
      ? `${window.location.origin}/sign-up`
      : "/sign-up";

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
      const role = (m.role || "viewer") as keyof typeof ROLE_CONFIG;
      acc[role] = (acc[role] || 0) + 1;
      return acc;
    },
    {} as Record<string, number>
  );

  const statCards = [
    { label: "Members", value: stats?.members ?? org.members.length, icon: Users, color: "text-blue-500" },
    { label: "Integrations", value: stats?.integrations ?? 0, icon: Plug, color: "text-green-500" },
    { label: "Incidents", value: stats?.incidents ?? 0, icon: Zap, color: "text-orange-500" },
    { label: "Chat Sessions", value: stats?.chatSessions ?? 0, icon: MessageSquare, color: "text-violet-500" },
  ];

  return (
    <div className="space-y-6">
      {/* Stats Row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {statCards.map((s) => {
          const Icon = s.icon;
          return (
            <Card key={s.label} className="border-border/50">
              <CardContent className="p-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm text-muted-foreground">{s.label}</span>
                  <Icon className={`h-4 w-4 ${s.color}`} />
                </div>
                <p className="text-2xl font-bold">{s.value}</p>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Role breakdown + Sign-up link side by side */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Role breakdown */}
        <Card className="border-border/50">
          <CardContent className="p-5">
            <h3 className="text-sm font-medium text-muted-foreground mb-4">Role Breakdown</h3>
            <div className="space-y-3">
              {(["admin", "editor", "viewer"] as const).map((role) => {
                const config = ROLE_CONFIG[role];
                const Icon = config.icon;
                const count = roleCounts[role] || 0;
                const pct = org.members.length > 0 ? (count / org.members.length) * 100 : 0;
                return (
                  <div key={role} className="space-y-1.5">
                    <div className="flex items-center justify-between text-sm">
                      <div className="flex items-center gap-2">
                        <div className={`h-7 w-7 rounded-md ${config.bg} flex items-center justify-center`}>
                          <Icon className={`h-3.5 w-3.5 ${config.color}`} />
                        </div>
                        <span className="font-medium">{config.label}</span>
                      </div>
                      <span className="text-muted-foreground">{count}</span>
                    </div>
                    <div className="h-1.5 bg-muted rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all duration-500 ${
                          role === "admin" ? "bg-purple-500" : role === "editor" ? "bg-amber-500" : "bg-blue-500"
                        }`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>

        {/* Invite / Sign-up link */}
        <Card className="border-border/50">
          <CardContent className="p-5">
            <h3 className="text-sm font-medium text-muted-foreground mb-2">Invite Team Members</h3>
            <p className="text-sm text-muted-foreground mb-4">
              Share this link with your team. New users join as{" "}
              <span className="text-blue-500 font-medium">Viewer</span> by default
              {isAdmin && " — you can promote them from the Members tab"}.
            </p>
            <div className="flex items-center gap-2 p-3 bg-muted/50 rounded-lg border border-border">
              <code className="text-sm flex-1 truncate select-all text-foreground/80">{signUpUrl}</code>
              <Button variant="ghost" size="sm" className="h-8 w-8 p-0 flex-shrink-0" onClick={copyLink}>
                {copied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>
            <div className="mt-4 flex items-center gap-3 text-xs text-muted-foreground">
              <div className="flex items-center gap-1">
                <span className="h-2 w-2 rounded-full bg-green-500" />
                Org slug: <span className="font-mono font-medium text-foreground/70">{org.slug}</span>
              </div>
              <span className="text-border">·</span>
              <span>
                Created{" "}
                {org.createdAt
                  ? new Date(org.createdAt).toLocaleDateString(undefined, {
                      month: "short",
                      day: "numeric",
                      year: "numeric",
                    })
                  : "recently"}
              </span>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

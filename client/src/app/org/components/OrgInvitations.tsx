"use client";

import { useState } from "react";
import { useSession } from "next-auth/react";
import { Loader2, Mail, CheckCircle, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "@/hooks/use-toast";
import { useQuery, jsonFetcher } from "@/lib/query";

interface Invitation {
  id: string;
  orgName: string;
  role: string;
  invitedBy: string | null;
  createdAt: string | null;
  expiresAt: string | null;
}

export default function OrgInvitations() {
  const { data: session } = useSession();
  const [acting, setActing] = useState<string | null>(null);

  const { data, isLoading, mutate } = useQuery<{ invitations: Invitation[] }>(
    session?.user?.id ? "/api/orgs/my-invitations" : null,
    jsonFetcher,
    { staleTime: 15_000, revalidateOnFocus: true },
  );

  const invitations = data?.invitations || [];

  async function handleAccept(inv: Invitation) {
    setActing(inv.id);
    try {
      const res = await fetch(`/api/orgs/my-invitations/${inv.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "accept" }),
      });
      const body = await res.json();
      if (!res.ok) {
        toast({ title: "Failed to join", description: body.error || "Something went wrong", variant: "destructive" });
        setActing(null);
        return;
      }

      // Redirect to a standalone transition page that has ZERO SWR hooks or
      // data-fetching components.  That page awaits the session refresh (which
      // rewrites the JWT cookie with the new org_id) and only then navigates
      // to "/".  This avoids the 403 storm caused by other mounted components
      // firing requests with the stale org while update() is in flight.
      window.location.replace("/org/switching");
    } catch {
      toast({ title: "Failed to join", variant: "destructive" });
      setActing(null);
    }
  }

  async function handleDecline(inv: Invitation) {
    setActing(inv.id);
    try {
      const res = await fetch(`/api/orgs/my-invitations/${inv.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "decline" }),
      });
      if (res.ok) {
        toast({ title: "Invitation declined" });
        mutate();
      } else {
        const body = await res.json().catch(() => ({}));
        toast({ title: "Failed", description: body.error || "Something went wrong", variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed to decline", variant: "destructive" });
    } finally {
      setActing(null);
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin mr-2" /> Loading invitations...
      </div>
    );
  }

  if (invitations.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <Mail className="h-8 w-8 text-muted-foreground/30 mb-2" />
        <p className="text-sm text-muted-foreground">No pending invitations</p>
        <p className="text-xs text-muted-foreground/60 mt-1">
          When someone invites you to their organization, it will appear here.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground mb-4">
        {invitations.length} pending invitation{invitations.length !== 1 ? "s" : ""}
      </p>
      {invitations.map((inv) => (
        <div
          key={inv.id}
          className="flex items-center justify-between p-4 rounded-lg border border-border bg-card"
        >
          <div className="min-w-0">
            <p className="text-sm font-medium truncate">{inv.orgName}</p>
            <p className="text-xs text-muted-foreground">
              {inv.invitedBy ? `Invited by ${inv.invitedBy}` : "Invitation"}
              {" · "}Role: <span className="capitalize">{inv.role}</span>
              {inv.expiresAt && (
                <>
                  {" · "}Expires{" "}
                  {new Date(inv.expiresAt).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                  })}
                </>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2 ml-4 flex-shrink-0">
            <Button
              size="sm"
              variant="outline"
              className="gap-1.5 h-8 text-destructive hover:text-destructive"
              onClick={() => handleDecline(inv)}
              disabled={acting === inv.id}
            >
              {acting === inv.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <XCircle className="h-3.5 w-3.5" />}
              Decline
            </Button>
            <Button
              size="sm"
              className="gap-1.5 h-8"
              onClick={() => handleAccept(inv)}
              disabled={acting === inv.id}
            >
              {acting === inv.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle className="h-3.5 w-3.5" />}
              Accept
            </Button>
          </div>
        </div>
      ))}
      <p className="text-xs text-muted-foreground/60 pt-2">
        Accepting an invitation will move you and your data to the new organization.
      </p>
    </div>
  );
}

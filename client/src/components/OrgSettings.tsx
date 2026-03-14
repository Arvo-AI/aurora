"use client";

import React, { useEffect, useState } from "react";
import { useUser } from "@/hooks/useAuthHooks";
import { useSession } from "next-auth/react";
import { isAdmin as checkAdmin } from "@/lib/roles";
import { Pencil, Check, X, Loader2 } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "@/hooks/use-toast";
import OrgOverview from "@/app/org/components/OrgOverview";
import OrgMembers from "@/app/org/components/OrgMembers";
import OrgIntegrations from "@/app/org/components/OrgIntegrations";
import OrgActivity from "@/app/org/components/OrgActivity";

interface OrgData {
  id: string;
  name: string;
  slug: string;
  createdBy: string;
  createdAt: string;
  members: OrgMember[];
}

interface OrgMember {
  id: string;
  email: string;
  name: string | null;
  role: string;
  createdAt: string | null;
}

export function OrgSettings() {
  const { user } = useUser();
  const { update: updateSession } = useSession();
  const [org, setOrg] = useState<OrgData | null>(null);
  const [loading, setLoading] = useState(true);
  const [editingName, setEditingName] = useState(false);
  const [nameInput, setNameInput] = useState("");
  const [savingName, setSavingName] = useState(false);

  const isAdmin = checkAdmin(user?.role);
  const editingNameRef = React.useRef(false);
  editingNameRef.current = editingName;

  useEffect(() => {
    if (user) fetchOrg();
  }, [user?.id]);

  async function fetchOrg() {
    try {
      const res = await fetch("/api/orgs/current");
      if (res.ok) {
        const data = await res.json();
        setOrg(data);
        if (!editingNameRef.current) {
          setNameInput(data.name);
        }
      }
    } catch (err) {
      console.error("Failed to fetch org:", err);
    } finally {
      setLoading(false);
    }
  }

  async function saveName() {
    const trimmed = nameInput.trim();
    if (!trimmed || trimmed === org?.name) {
      setEditingName(false);
      return;
    }
    setSavingName(true);
    try {
      const res = await fetch("/api/orgs/current", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      });
      const data = await res.json();
      if (res.ok) {
        setOrg((prev) => (prev ? { ...prev, name: data.name } : prev));
        setNameInput(data.name);
        toast({ title: "Name updated" });
        await updateSession();
      } else {
        toast({ title: data.error || "Failed to update name", variant: "destructive" });
        setNameInput(org?.name || "");
      }
    } catch {
      toast({ title: "Failed to update", variant: "destructive" });
      setNameInput(org?.name || "");
    } finally {
      setSavingName(false);
      setEditingName(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading...
      </div>
    );
  }

  if (!org) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground text-sm">
        No organization found.
      </div>
    );
  }

  const initial = org.name.charAt(0).toUpperCase();

  return (
    <div className="space-y-6">
      {/* Org header */}
      <div>
        <div className="flex items-center gap-3 mb-1">
          <div className="h-9 w-9 rounded-lg bg-foreground text-background flex items-center justify-center text-base font-semibold select-none flex-shrink-0">
            {initial}
          </div>
          {editingName ? (
            <div className="flex items-center gap-2">
              <Input
                value={nameInput}
                onChange={(e) => setNameInput(e.target.value)}
                className="text-lg font-semibold h-8 max-w-xs border-none shadow-none focus-visible:ring-1 px-2"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") saveName();
                  if (e.key === "Escape") { setEditingName(false); setNameInput(org.name); }
                }}
              />
              <Button size="icon" variant="ghost" className="h-7 w-7" onClick={saveName} disabled={savingName}>
                <Check className="h-3.5 w-3.5" />
              </Button>
              <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => { setEditingName(false); setNameInput(org.name); }}>
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
          ) : (
            <button
              onClick={isAdmin ? () => setEditingName(true) : undefined}
              className={`group flex items-center gap-2 text-lg font-semibold tracking-tight ${isAdmin ? "hover:text-muted-foreground transition-colors cursor-text" : "cursor-default"}`}
            >
              {org.name}
              {isAdmin && (
                <Pencil className="h-3.5 w-3.5 text-muted-foreground/0 group-hover:text-muted-foreground transition-colors" />
              )}
            </button>
          )}
        </div>
        <p className="text-[13px] text-muted-foreground ml-12">
          <span className="font-mono text-xs text-muted-foreground/60">{org.slug}</span>
          <span className="mx-2 text-border">·</span>
          {org.members.length} member{org.members.length !== 1 ? "s" : ""}
          <span className="mx-2 text-border">·</span>
          since{" "}
          {org.createdAt
            ? new Date(org.createdAt).toLocaleDateString(undefined, { month: "short", year: "numeric" })
            : "recently"}
        </p>
      </div>

      {/* Org tabs */}
      <Tabs defaultValue="overview" className="w-full">
        <div className="border-b border-border mb-6">
          <TabsList className="h-auto p-0 bg-transparent rounded-none gap-5">
            {["overview", "members", "integrations", "activity"].map((tab) => (
              <TabsTrigger
                key={tab}
                value={tab}
                className="px-0 pb-2 pt-0 rounded-none border-b-2 border-transparent data-[state=active]:border-foreground data-[state=active]:bg-transparent data-[state=active]:shadow-none text-muted-foreground data-[state=active]:text-foreground capitalize text-sm font-medium"
              >
                {tab}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="overview">
          <OrgOverview org={org} isAdmin={isAdmin} />
        </TabsContent>
        <TabsContent value="members">
          <OrgMembers org={org} currentUserId={user?.id || ""} isAdmin={isAdmin} onMembersChanged={fetchOrg} />
        </TabsContent>
        <TabsContent value="integrations">
          <OrgIntegrations />
        </TabsContent>
        <TabsContent value="activity">
          <OrgActivity />
        </TabsContent>
      </Tabs>
    </div>
  );
}

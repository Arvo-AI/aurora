"use client";

import { useState, useCallback, Fragment } from "react";
import {
  Eye,
  Pencil,
  Crown,
  Check,
  Minus,
  Plus,
  Loader2,
  ChevronDown,
  UserMinus,
  Users,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { toast } from "@/hooks/use-toast";
import type { OrgMember } from "../page";

const VALID_ROLES = ["admin", "editor", "viewer"] as const;

const ROLE_INFO = {
  viewer: {
    icon: Eye,
    color: "text-blue-500",
    bg: "bg-blue-500/10",
    badge: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200",
    label: "Viewer",
    summary: "Read-only access",
  },
  editor: {
    icon: Pencil,
    color: "text-amber-500",
    bg: "bg-amber-500/10",
    badge: "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-200",
    label: "Editor",
    summary: "Read + write access",
  },
  admin: {
    icon: Crown,
    color: "text-purple-500",
    bg: "bg-purple-500/10",
    badge: "bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200",
    label: "Admin",
    summary: "Full access + user management",
  },
} as const;

type PermAccess = boolean;

const PERMISSION_TABLE: {
  category: string;
  features: { name: string; viewer: PermAccess; editor: PermAccess; admin: PermAccess }[];
}[] = [
  {
    category: "Incidents",
    features: [
      { name: "View incidents & alerts", viewer: true, editor: true, admin: true },
      { name: "Update & resolve incidents", viewer: false, editor: true, admin: true },
      { name: "Apply suggestions & merge", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Postmortems",
    features: [
      { name: "View postmortems", viewer: true, editor: true, admin: true },
      { name: "Edit & export postmortems", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Chat & Knowledge Base",
    features: [
      { name: "Use the chat assistant", viewer: true, editor: true, admin: true },
      { name: "View knowledge base", viewer: true, editor: true, admin: true },
      { name: "Upload & manage documents", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Integrations",
    features: [
      { name: "View connector status", viewer: true, editor: true, admin: true },
      { name: "Connect & disconnect", viewer: false, editor: true, admin: true },
      { name: "Manage SSH keys & VMs", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Infrastructure",
    features: [
      { name: "View service graph", viewer: true, editor: true, admin: true },
      { name: "Edit services & dependencies", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Administration",
    features: [
      { name: "Configure LLM providers", viewer: false, editor: false, admin: true },
      { name: "Manage users & roles", viewer: false, editor: false, admin: true },
      { name: "Organization settings", viewer: false, editor: false, admin: true },
    ],
  },
];

function PermCell({ allowed }: { allowed: boolean }) {
  return allowed ? (
    <Check className="h-4 w-4 text-green-500" />
  ) : (
    <Minus className="h-4 w-4 text-muted-foreground/30" />
  );
}

function RoleBadge({ role }: { role: string }) {
  const info = ROLE_INFO[role as keyof typeof ROLE_INFO];
  if (!info) return <Badge variant="outline">{role}</Badge>;
  const Icon = info.icon;
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium ${info.badge}`}
    >
      <Icon className="h-3 w-3" />
      {info.label}
    </span>
  );
}

function AddUserDialog({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [role, setRole] = useState<string>("viewer");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  function reset() {
    setName(""); setEmail(""); setPassword(""); setConfirmPassword(""); setRole("viewer"); setError("");
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!email || !password) { setError("Email and password are required"); return; }
    if (password.length < 8) { setError("Password must be at least 8 characters"); return; }
    if (password !== confirmPassword) { setError("Passwords do not match"); return; }

    setSaving(true);
    try {
      const res = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, name, role }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Failed to create user"); return; }
      const roleLabel = ROLE_INFO[role as keyof typeof ROLE_INFO]?.label || role;
      toast({ title: "User created", description: `${name || email} added as ${roleLabel}` });
      reset();
      setOpen(false);
      onCreated();
    } catch {
      setError("Something went wrong");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) reset(); }}>
      <DialogTrigger asChild>
        <Button size="sm" className="gap-1.5">
          <Plus className="h-4 w-4" />
          Add Member
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={handleCreate}>
          <DialogHeader>
            <DialogTitle>Add team member</DialogTitle>
            <DialogDescription>
              Create an account for a new team member. They can sign in with these credentials.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="add-name">Name</Label>
              <Input id="add-name" placeholder="Jane Smith" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="add-email">Email</Label>
              <Input id="add-email" type="email" placeholder="jane@company.com" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="add-password">Password</Label>
              <Input id="add-password" type="password" placeholder="Min 8 characters" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="add-confirm-pw">Confirm password</Label>
              <Input id="add-confirm-pw" type="password" placeholder="Re-enter password" required value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label>Role</Label>
              <Select value={role} onValueChange={setRole}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {VALID_ROLES.map((r) => {
                    const info = ROLE_INFO[r];
                    const Icon = info.icon;
                    return (
                      <SelectItem key={r} value={r}>
                        <span className="flex items-center gap-2">
                          <Icon className={`h-3.5 w-3.5 ${info.color}`} />
                          {info.label}
                          <span className="text-muted-foreground text-xs ml-1">— {info.summary}</span>
                        </span>
                      </SelectItem>
                    );
                  })}
                </SelectContent>
              </Select>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
          </div>
          <DialogFooter>
            <Button type="submit" disabled={saving} className="gap-2">
              {saving && <Loader2 className="h-4 w-4 animate-spin" />}
              Create member
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

interface OrgMembersProps {
  org: { id: string; name: string; members: OrgMember[] };
  currentUserId: string;
  isAdmin: boolean;
  onMembersChanged: () => void;
}

export default function OrgMembers({ org, currentUserId, isAdmin, onMembersChanged }: OrgMembersProps) {
  const [updating, setUpdating] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [permOpen, setPermOpen] = useState(false);

  const handleRoleChange = useCallback(
    async (targetUserId: string, newRole: string) => {
      setUpdating(targetUserId);
      const target = org.members.find((m) => m.id === targetUserId);
      try {
        const res = await fetch(`/api/admin/users/${targetUserId}/roles`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: newRole }),
        });
        if (res.ok) {
          const displayName = target?.name || target?.email || "User";
          const newLabel = ROLE_INFO[newRole as keyof typeof ROLE_INFO]?.label || newRole;
          toast({ title: "Role updated", description: `${displayName} is now ${newLabel}` });
          onMembersChanged();
        } else {
          const data = await res.json().catch(() => ({}));
          toast({ title: "Failed to update role", description: data.error || "Something went wrong", variant: "destructive" });
        }
      } catch {
        toast({ title: "Failed to update role", description: "Could not reach the server", variant: "destructive" });
      } finally {
        setUpdating(null);
      }
    },
    [org.members, onMembersChanged]
  );

  async function handleRemove(targetUserId: string) {
    const target = org.members.find((m) => m.id === targetUserId);
    setRemoving(targetUserId);
    try {
      const res = await fetch(`/api/orgs/current`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ targetUserId }),
      });
      if (res.ok) {
        toast({ title: "Member removed", description: `${target?.name || target?.email} has been removed` });
        onMembersChanged();
      }
    } catch {
      toast({ title: "Failed to remove member", variant: "destructive" });
    } finally {
      setRemoving(null);
    }
  }

  function getInitials(member: OrgMember) {
    if (member.name) {
      return member.name.split(" ").map((n) => n[0]).join("").toUpperCase().slice(0, 2);
    }
    return member.email.slice(0, 2).toUpperCase();
  }

  return (
    <div className="space-y-4">
      {/* Header with Add button */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Team Members</h2>
          <p className="text-sm text-muted-foreground">
            {org.members.length} member{org.members.length !== 1 ? "s" : ""} in {org.name}
          </p>
        </div>
        {isAdmin && <AddUserDialog onCreated={onMembersChanged} />}
      </div>

      {/* Member list */}
      <div className="border border-border rounded-lg overflow-hidden">
        {org.members.map((member, idx) => (
          <div
            key={member.id}
            className={`flex items-center gap-4 px-4 py-3 hover:bg-muted/30 transition-colors ${
              idx < org.members.length - 1 ? "border-b border-border/50" : ""
            }`}
          >
            <Avatar className="h-9 w-9">
              <AvatarFallback className="bg-primary/10 text-primary text-xs font-medium">
                {getInitials(member)}
              </AvatarFallback>
            </Avatar>

            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium truncate">
                  {member.name || member.email}
                </span>
                {member.id === currentUserId && (
                  <span className="text-xs text-muted-foreground bg-muted px-1.5 py-0.5 rounded">you</span>
                )}
              </div>
              {member.name && (
                <p className="text-xs text-muted-foreground truncate">{member.email}</p>
              )}
            </div>

            <div className="flex items-center gap-2">
              {isAdmin && member.id !== currentUserId ? (
                <Select
                  value={member.role}
                  onValueChange={(val) => handleRoleChange(member.id, val)}
                  disabled={updating === member.id}
                >
                  <SelectTrigger className="w-32 h-8 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {VALID_ROLES.map((r) => {
                      const info = ROLE_INFO[r];
                      const Icon = info.icon;
                      return (
                        <SelectItem key={r} value={r} className="text-sm">
                          <span className="flex items-center gap-2">
                            <Icon className={`h-3.5 w-3.5 ${info.color}`} />
                            {info.label}
                          </span>
                        </SelectItem>
                      );
                    })}
                  </SelectContent>
                </Select>
              ) : (
                <RoleBadge role={member.role} />
              )}

              {isAdmin && member.id !== currentUserId && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-destructive"
                  onClick={() => handleRemove(member.id)}
                  disabled={removing === member.id}
                >
                  {removing === member.id ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <UserMinus className="h-4 w-4" />
                  )}
                </Button>
              )}
            </div>

            <span className="text-xs text-muted-foreground w-24 text-right hidden sm:block">
              {member.createdAt
                ? new Date(member.createdAt).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                    year: "numeric",
                  })
                : "—"}
            </span>
          </div>
        ))}

        {org.members.length === 0 && (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <Users className="h-10 w-10 text-muted-foreground/40 mb-3" />
            <p className="text-sm text-muted-foreground">No members yet</p>
          </div>
        )}
      </div>

      {/* Permission matrix accordion */}
      <Collapsible open={permOpen} onOpenChange={setPermOpen}>
        <CollapsibleTrigger asChild>
          <Button variant="ghost" className="w-full justify-between text-sm text-muted-foreground hover:text-foreground h-10 px-3">
            <span>Role permissions reference</span>
            <ChevronDown className={`h-4 w-4 transition-transform ${permOpen ? "rotate-180" : ""}`} />
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="rounded-lg border border-border overflow-hidden text-sm mt-2">
            <table className="w-full">
              <thead>
                <tr className="bg-muted/50">
                  <th className="text-left px-3 py-2.5 font-medium text-muted-foreground w-[55%]">Feature</th>
                  {(["viewer", "editor", "admin"] as const).map((role) => {
                    const info = ROLE_INFO[role];
                    const Icon = info.icon;
                    return (
                      <th key={role} className="text-center px-2 py-2.5 font-medium w-[15%]">
                        <div className="flex flex-col items-center gap-1">
                          <Icon className={`h-4 w-4 ${info.color}`} />
                          <span className="text-xs">{info.label}</span>
                        </div>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {PERMISSION_TABLE.map((section) => (
                  <Fragment key={section.category}>
                    <tr>
                      <td
                        colSpan={4}
                        className="px-3 py-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider bg-muted/30 border-t border-border"
                      >
                        {section.category}
                      </td>
                    </tr>
                    {section.features.map((feat) => (
                      <tr key={feat.name} className="border-t border-border/50">
                        <td className="px-3 py-2 text-foreground/80">{feat.name}</td>
                        <td className="text-center px-2 py-2"><PermCell allowed={feat.viewer} /></td>
                        <td className="text-center px-2 py-2"><PermCell allowed={feat.editor} /></td>
                        <td className="text-center px-2 py-2"><PermCell allowed={feat.admin} /></td>
                      </tr>
                    ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}


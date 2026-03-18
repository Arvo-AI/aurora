"use client";

import { useState, useCallback, Fragment } from "react";
import {
  Check,
  Minus,
  Plus,
  Loader2,
  ChevronDown,
  UserMinus,
  Users,
} from "lucide-react";
import { Button } from "@/components/ui/button";
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
import { toast } from "@/hooks/use-toast";
import { VALID_ROLES, ROLE_META, type UserRole } from "@/lib/roles";
import type { OrgMember } from "../page";

const PERMISSION_TABLE: {
  category: string;
  features: { name: string; viewer: boolean; editor: boolean; admin: boolean }[];
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
      { name: "Upload & manage documents", viewer: false, editor: true, admin: true },
    ],
  },
  {
    category: "Integrations",
    features: [
      { name: "View connector status", viewer: true, editor: true, admin: true },
      { name: "Connect & disconnect", viewer: false, editor: true, admin: true },
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
      toast({ title: "Member added", description: `${name || email} joined as ${role}` });
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
        <Button variant="outline" size="sm" className="gap-1.5 h-8">
          <Plus className="h-3.5 w-3.5" />
          Add
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={handleCreate}>
          <DialogHeader>
            <DialogTitle>Add team member</DialogTitle>
            <DialogDescription>
              Create an account. They&apos;ll sign in with these credentials.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-1.5">
              <Label htmlFor="add-name" className="text-xs">Name</Label>
              <Input id="add-name" placeholder="Jane Smith" value={name} onChange={(e) => setName(e.target.value)} className="h-9" />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="add-email" className="text-xs">Email</Label>
              <Input id="add-email" type="email" placeholder="jane@company.com" required value={email} onChange={(e) => setEmail(e.target.value)} className="h-9" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-1.5">
                <Label htmlFor="add-pw" className="text-xs">Temporary Password</Label>
                <Input id="add-pw" type="password" placeholder="Min 8 chars" required value={password} onChange={(e) => setPassword(e.target.value)} className="h-9" />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="add-cpw" className="text-xs">Confirm</Label>
                <Input id="add-cpw" type="password" placeholder="Re-enter" required value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} className="h-9" />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">The user will be required to change this password on their first login.</p>
            <div className="grid gap-1.5">
              <Label className="text-xs">Role</Label>
              <Select value={role} onValueChange={setRole}>
                <SelectTrigger className="h-9 w-full [&>span]:line-clamp-none"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {VALID_ROLES.map((r) => (
                    <SelectItem key={r} value={r}>
                      <span className="flex items-center gap-2">
                        {ROLE_META[r].label}
                        <span className="text-muted-foreground text-xs">— {ROLE_META[r].desc}</span>
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
          </div>
          <DialogFooter>
            <Button type="submit" disabled={saving} size="sm" className="gap-2">
              {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Create
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
          toast({ title: `${target?.name || target?.email} is now ${newRole}` });
          onMembersChanged();
        } else {
          const data = await res.json().catch(() => ({}));
          toast({ title: "Failed", description: data.error || "Something went wrong", variant: "destructive" });
        }
      } catch {
        toast({ title: "Failed", description: "Could not reach server", variant: "destructive" });
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
      const res = await fetch(`/api/orgs/current/members/${targetUserId}`, {
        method: "DELETE",
      });
      if (res.ok) {
        toast({ title: `${target?.name || target?.email} removed` });
        onMembersChanged();
      }
    } catch {
      toast({ title: "Failed to remove", variant: "destructive" });
    } finally {
      setRemoving(null);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {org.members.length} member{org.members.length !== 1 ? "s" : ""}
        </p>
        {isAdmin && <AddUserDialog onCreated={onMembersChanged} />}
      </div>

      {/* Clean table — no heavy borders, just rows */}
      <div className="text-sm">
        <div className="grid grid-cols-[1fr_100px_100px_auto] gap-x-4 px-1 pb-2 text-xs text-muted-foreground font-medium border-b border-border">
          <span>Name</span>
          <span>Role</span>
          <span>Joined</span>
          <span className="w-8" />
        </div>

        {org.members.map((member) => (
          <div
            key={member.id}
            className="grid grid-cols-[1fr_100px_100px_auto] gap-x-4 items-center px-1 py-3 border-b border-border/40 last:border-0 group"
          >
            {/* Name + email */}
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className="h-6 w-6 rounded-full bg-muted flex items-center justify-center text-[10px] font-medium text-muted-foreground flex-shrink-0">
                  {(member.name || member.email).charAt(0).toUpperCase()}
                </div>
                <span className="font-medium truncate">{member.name || member.email}</span>
                {member.id === currentUserId && (
                  <span className="text-[10px] text-muted-foreground/60 font-normal">you</span>
                )}
              </div>
              {member.name && (
                <p className="text-xs text-muted-foreground truncate ml-8">{member.email}</p>
              )}
            </div>

            {/* Role */}
            <div>
              {isAdmin && member.id !== currentUserId ? (
                <Select
                  value={member.role}
                  onValueChange={(val) => handleRoleChange(member.id, val)}
                  disabled={updating === member.id}
                >
                  <SelectTrigger className="w-24 h-7 text-xs border-transparent hover:border-border transition-colors">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {VALID_ROLES.map((r) => (
                      <SelectItem key={r} value={r} className="text-xs">{ROLE_META[r].label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <span className="text-xs text-muted-foreground capitalize">{member.role}</span>
              )}
            </div>

            {/* Joined */}
            <span className="text-xs text-muted-foreground tabular-nums">
              {member.createdAt
                ? new Date(member.createdAt).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                    year: "numeric",
                  })
                : "—"}
            </span>

            {/* Remove */}
            <div className="w-8 flex justify-end">
              {isAdmin && member.id !== currentUserId ? (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                  onClick={() => handleRemove(member.id)}
                  disabled={removing === member.id}
                >
                  {removing === member.id ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <UserMinus className="h-3 w-3" />
                  )}
                </Button>
              ) : null}
            </div>
          </div>
        ))}

        {org.members.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <Users className="h-8 w-8 text-muted-foreground/30 mb-2" />
            <p className="text-sm text-muted-foreground">No members yet</p>
          </div>
        )}
      </div>

      {/* Permissions reference — collapsed by default */}
      <Collapsible open={permOpen} onOpenChange={setPermOpen}>
        <CollapsibleTrigger asChild>
          <button className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors py-1">
            <ChevronDown className={`h-3 w-3 transition-transform ${permOpen ? "rotate-180" : ""}`} />
            Permissions reference
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="mt-3 text-xs">
            <div className="grid grid-cols-[1fr_60px_60px_60px] gap-x-2 pb-2 font-medium text-muted-foreground border-b border-border">
              <span />
              <span className="text-center">View</span>
              <span className="text-center">Edit</span>
              <span className="text-center">Admin</span>
            </div>
            {PERMISSION_TABLE.map((section) => (
              <Fragment key={section.category}>
                <div className="pt-3 pb-1 text-[10px] font-semibold text-muted-foreground/60 uppercase tracking-widest">
                  {section.category}
                </div>
                {section.features.map((feat) => (
                  <div key={feat.name} className="grid grid-cols-[1fr_60px_60px_60px] gap-x-2 py-1.5 border-b border-border/30 text-muted-foreground">
                    <span>{feat.name}</span>
                    <span className="flex justify-center">{feat.viewer ? <Check className="h-3 w-3 text-foreground/50" /> : <Minus className="h-3 w-3 text-border" />}</span>
                    <span className="flex justify-center">{feat.editor ? <Check className="h-3 w-3 text-foreground/50" /> : <Minus className="h-3 w-3 text-border" />}</span>
                    <span className="flex justify-center">{feat.admin ? <Check className="h-3 w-3 text-foreground/50" /> : <Minus className="h-3 w-3 text-border" />}</span>
                  </div>
                ))}
              </Fragment>
            ))}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}

"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Loader2, Plus, Users, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/hooks/use-toast";

export interface Team { id: string; name: string; memberIds: string[]; grants: { connector: string; scopeId: string }[] }

const NONE = "none";

export function teamIdFor(teams: Team[], userId: string): string {
  return teams.find((team) => team.memberIds.includes(userId))?.id ?? NONE;
}

export function useOrgTeams() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/org/teams");
      if (!res.ok) throw new Error("load failed");
      const data = await res.json();
      setTeams(data.teams ?? []);
    } catch {
      toast({ title: "Could not load teams", variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function create(name: string) {
    const trimmed = name.trim();
    if (!trimmed) return false;
    setBusy("new");
    try {
      const res = await fetch("/api/org/teams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      });
      if (!res.ok) throw new Error("create failed");
      await load();
      return true;
    } catch {
      toast({ title: "Could not create team", variant: "destructive" });
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function remove(team: Team) {
    setBusy(team.id);
    try {
      const res = await fetch(`/api/org/teams/${encodeURIComponent(team.id)}`, { method: "DELETE" });
      if (!res.ok) throw new Error("delete failed");
      setTeams((prev) => prev.filter((item) => item.id !== team.id));
    } catch {
      toast({ title: `Could not delete ${team.name}`, variant: "destructive" });
    } finally {
      setBusy(null);
    }
  }

  async function assign(userId: string, teamId: string) {
    const nextId = teamId === NONE ? null : teamId;
    const previous = teams;
    setBusy(userId);
    setTeams((prev) => prev.map((team) => {
      const without = team.memberIds.filter((id) => id !== userId);
      // One team per person, so the chosen row is the only one that keeps them.
      return { ...team, memberIds: team.id === nextId ? [...without, userId] : without };
    }));
    try {
      const res = await fetch("/api/org/teams/membership", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ userId, teamId: nextId }),
      });
      if (!res.ok) throw new Error("save failed");
    } catch {
      setTeams(previous);
      toast({ title: "Could not update team", variant: "destructive" });
    } finally {
      setBusy(null);
    }
  }

  return { teams, loading, busy, create, remove, assign };
}

function CreateTeamDialog({ onCreate, busy }: { onCreate: (name: string) => Promise<boolean>; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    const ok = await onCreate(name);
    if (!ok) return;
    setName("");
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={(value) => { setOpen(value); if (!value) setName(""); }}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5 h-8">
          <Plus className="h-3.5 w-3.5" />
          Add
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={(event) => void submit(event)}>
          <DialogHeader>
            <DialogTitle>Create team</DialogTitle>
            <DialogDescription>
              Name the team. Assign people from the member list.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-1.5">
              <Label htmlFor="team-name" className="text-xs">Name</Label>
              <Input
                id="team-name"
                placeholder="Networking"
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="h-9"
                autoFocus
              />
            </div>
          </div>
          <DialogFooter>
            <Button type="submit" disabled={busy || !name.trim()} size="sm" className="gap-2">
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Create team
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function TeamList({
  teams,
  busy,
  onCreate,
  onRemove,
}: {
  teams: Team[];
  busy: string | null;
  onCreate: (name: string) => Promise<boolean>;
  onRemove: (team: Team) => void;
}) {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {teams.length} team{teams.length === 1 ? "" : "s"}
        </p>
        <CreateTeamDialog onCreate={onCreate} busy={busy === "new"} />
      </div>
      <div className="text-sm">
        <div className="grid grid-cols-[1fr_100px_auto] gap-x-4 px-1 pb-2 text-xs text-muted-foreground font-medium border-b border-border">
          <span>Name</span>
          <span>Members</span>
          <span className="w-8" />
        </div>
        {teams.map((team) => (
          <div
            key={team.id}
            className="grid grid-cols-[1fr_100px_auto] gap-x-4 items-center px-1 py-3 border-b border-border/40 last:border-0 group"
          >
            <div className="flex items-center gap-2 min-w-0">
              <div className="h-6 w-6 rounded-full bg-muted flex items-center justify-center text-[10px] font-medium text-muted-foreground flex-shrink-0">
                {team.name.charAt(0).toUpperCase()}
              </div>
              <span className="font-medium truncate">{team.name}</span>
            </div>
            <span className="text-xs text-muted-foreground tabular-nums">{team.memberIds.length}</span>
            <div className="w-8 flex justify-end">
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                onClick={() => onRemove(team)}
                disabled={busy === team.id}
                aria-label={`Delete ${team.name}`}
              >
                {busy === team.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <X className="h-3 w-3" />}
              </Button>
            </div>
          </div>
        ))}
        {teams.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <Users className="h-8 w-8 text-muted-foreground/30 mb-2" />
            <p className="text-sm text-muted-foreground">No teams yet</p>
          </div>
        )}
      </div>
    </div>
  );
}

export { NONE as NO_TEAM };

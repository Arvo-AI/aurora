"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Check, Loader2, MessageSquare } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry";
import { toast } from "@/hooks/use-toast";
import { ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, type UserRole } from "@/lib/roles";

const ROLES: { id: UserRole; label: string }[] = [
  { id: ROLE_VIEWER, label: "Viewer" },
  { id: ROLE_EDITOR, label: "Editor" },
  { id: ROLE_ADMIN, label: "Admin" },
];

const CATEGORY_ORDER = [
  "Monitoring",
  "Incident Management",
  "Infrastructure",
  "Networking",
  "Development",
  "Documentation",
  "Communication",
  "CI/CD",
  "Custom MCP",
];

const GRID = "grid grid-cols-[minmax(0,1fr)_5.75rem_5.75rem_5.75rem] gap-x-2 items-center";
const EVERYONE = "everyone";

interface Credential { id: string; label: string }
interface Scope { id: string; label: string; credentials: Credential[] }
interface Grant { scopeId: string; credential: string; role: string }
interface Connector {
  id: string;
  name: string;
  kind: string;
  limited: boolean;
  inherited?: boolean;
  scopes: Scope[];
  grants: Grant[];
}
interface AudienceTeam { id: string; name: string }

interface ViewerRun { chat: boolean }

function isFlat(connector: Connector): boolean {
  return connector.scopes.length === 1
    && connector.scopes[0].credentials.length === 1
    && connector.scopes[0].credentials[0].id === "";
}

function categoryOf(connector: Connector): string {
  // Custom servers share one logo; group them apart from built-in connectors
  if (connector.kind === "mcp" || connector.id.startsWith("mcp:")) return "Custom MCP";
  return connectorRegistry.get(connector.id)?.category ?? "Other";
}

function subtitle(connector: Connector): string | null {
  if (connector.id.startsWith("mcp:")) return "Custom MCP";
  if (isFlat(connector)) return null;
  // Several accounts get their own rows, so the parent doesn't repeat the count
  if (connector.scopes.length > 1) return null;
  return connector.scopes[0].label || null;
}

function isPermissionScope(scope: Scope): boolean {
  return scope.credentials.some((c) => c.id === "write" || c.id === "read");
}

function permissionCredentials(scope: Scope): Credential[] {
  const order = ["write", "read"];
  return scope.credentials
    .filter((c) => c.id === "write" || c.id === "read")
    .sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
}

function preferredCredential(scope: Scope): string {
  const ids = new Set(scope.credentials.map((c) => c.id));
  // Write matches an open connector: Agent mode can modify, Ask mode stays read-only
  if (ids.has("write")) return "write";
  return scope.credentials[0]?.id ?? "";
}

function grantedCredential(connector: Connector, scopeId: string, roleId: string): string | null {
  if (!connector.limited) {
    const scope = connector.scopes.find((s) => s.id === scopeId);
    return scope ? preferredCredential(scope) : null;
  }
  const grant = connector.grants.find((g) => g.scopeId === scopeId && g.role === roleId);
  return grant ? grant.credential : null;
}

function credentialChecked(connector: Connector, scope: Scope, roleId: string, credId: string): boolean {
  const current = grantedCredential(connector, scope.id, roleId);
  if (current == null) return false;
  // Modify includes read-only, since Ask mode still uses the read-only role
  if (credId === "read") return current === "read" || current === "write";
  return current === credId;
}

function cellKey(scopeId: string, roleId: string): string {
  return `${scopeId}:${roleId}`;
}

/** Grants the checks currently show. An open connector is every role, on every account. */
function shownGrants(connector: Connector): Grant[] {
  if (!connector.limited) {
    return connector.scopes.flatMap((scope) =>
      ROLES.map((role) => ({
        scopeId: scope.id,
        credential: preferredCredential(scope),
        role: role.id,
      })),
    );
  }
  return connector.grants;
}

function grantsEqual(a: Grant[], b: Grant[]): boolean {
  const key = (g: Grant) => `${g.scopeId}/${g.credential}/${g.role}`;
  const ordered = (grants: Grant[]) => grants.map(key).sort((x, y) => x.localeCompare(y)).join(",");
  return ordered(a) === ordered(b);
}

function isChecked(connector: Connector, scopeId: string, roleId: string): boolean {
  return grantedCredential(connector, scopeId, roleId) != null;
}

function roleCheck(connector: Connector, roleId: string): boolean | "indeterminate" {
  const on = connector.scopes.filter((scope) => isChecked(connector, scope.id, roleId)).length;
  if (on === 0) return false;
  if (on === connector.scopes.length) return true;
  return "indeterminate";
}

function withCells(connector: Connector, updates: { scopeId: string; roleId: string; credential: string | null }[]): { limited: boolean; grants: Grant[] } {
  const byCell = new Map(shownGrants(connector).map((g) => [cellKey(g.scopeId, g.role), g]));
  for (const update of updates) {
    const key = cellKey(update.scopeId, update.roleId);
    if (update.credential == null) {
      byCell.delete(key);
      continue;
    }
    byCell.set(key, {
      scopeId: update.scopeId,
      credential: update.credential,
      role: update.roleId,
    });
  }
  const open = connector.scopes.every((scope) =>
    ROLES.every((role) => byCell.get(cellKey(scope.id, role.id))?.credential === preferredCredential(scope)),
  );
  // Every role on full access is the open state, so chat mode keeps picking the identity
  if (open) return { limited: false, grants: [] };
  return { limited: true, grants: [...byCell.values()] };
}

function nextCredential(scope: Scope, current: string | null, credId: string, on: boolean): string | null {
  const ids = new Set(scope.credentials.map((c) => c.id));
  if (credId === "write") {
    if (on) return "write";
    // Taking away modify leaves the read-only role when this account has one
    return ids.has("read") ? "read" : null;
  }
  if (credId === "read") {
    if (!on) return null;
    // Checking read-only does not take modify away
    return current === "write" ? "write" : "read";
  }
  return on ? credId : null;
}

function groupConnectors(connectors: Connector[]): [string, Connector[]][] {
  const groups = new Map<string, Connector[]>();
  for (const connector of connectors) {
    const category = categoryOf(connector);
    const list = groups.get(category) ?? [];
    list.push(connector);
    groups.set(category, list);
  }
  for (const list of groups.values()) {
    list.sort((a, b) => a.name.localeCompare(b.name));
  }
  return [...groups.entries()].sort(([a], [b]) => {
    const ia = CATEGORY_ORDER.indexOf(a);
    const ib = CATEGORY_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
}

function ConnectorLogo({ id }: { id: string }) {
  const config = connectorRegistry.get(id.startsWith("mcp:") ? "mcp" : id);
  const Icon = config?.icon;
  return (
    <div className={`flex h-6 w-6 flex-shrink-0 items-center justify-center overflow-hidden rounded-md border border-border/40 ${config?.iconBgColor || "bg-muted"}`}>
      {config?.iconPath ? (
        <img src={config.iconPath} alt="" className={`h-4 w-4 object-contain ${config.iconClassName || ""}`} />
      ) : Icon ? (
        <Icon className={`h-3.5 w-3.5 ${config?.iconColor || "text-foreground"}`} />
      ) : (
        <span className="text-[10px] font-medium text-muted-foreground">{id.charAt(0).toUpperCase()}</span>
      )}
    </div>
  );
}

function AccessCheck({
  checked, disabled, label, onChange,
}: {
  checked: boolean | "indeterminate";
  disabled: boolean;
  label: string;
  onChange: (on: boolean) => void;
}) {
  return (
    <label className={`flex h-8 items-center justify-center rounded-md ${disabled ? "cursor-not-allowed" : "cursor-pointer hover:bg-muted/60"}`}>
      <Checkbox
        checked={checked}
        disabled={disabled}
        aria-label={label}
        onCheckedChange={(value) => onChange(value === true)}
      />
    </label>
  );
}

function ConnectorRows({
  connector, disabled, onSave,
}: {
  connector: Connector;
  disabled: boolean;
  onSave: (limited: boolean, grants: Grant[]) => void;
}) {
  const permissions = connector.scopes.some(isPermissionScope);
  const note = permissions ? null : subtitle(connector);
  const perAccount = !permissions && !isFlat(connector) && connector.scopes.length > 1;

  function saveCells(updates: { scopeId: string; roleId: string; credential: string | null }[]) {
    const next = withCells(connector, updates);
    onSave(next.limited, next.grants);
  }

  return (
    <>
      <div className={`${GRID} px-1 py-1.5 ${permissions ? "" : "border-b border-border/30"}`}>
        <div className="flex min-w-0 items-center gap-2">
          <ConnectorLogo id={connector.id} />
          <div className="min-w-0">
            <span className="flex items-center gap-1.5 truncate font-medium">
              {connector.name}
              {connector.inherited === false && (
                <span className="text-[10px] font-normal uppercase tracking-widest text-muted-foreground">Custom</span>
              )}
              {disabled && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
            </span>
            {note && <p className="truncate text-xs text-muted-foreground">{note}</p>}
          </div>
        </div>
        {permissions ? (
          ROLES.map((role) => <span key={role.id} />)
        ) : (
          ROLES.map((role) => (
            <AccessCheck
              key={role.id}
              checked={roleCheck(connector, role.id)}
              disabled={disabled}
              label={`${role.label} can use ${connector.name}`}
              onChange={(on) => saveCells(connector.scopes.map((scope) => ({
                scopeId: scope.id,
                roleId: role.id,
                credential: on ? preferredCredential(scope) : null,
              })))}
            />
          ))
        )}
      </div>

      {permissions && connector.scopes.map((scope) => (
        <Fragment key={scope.id || "all"}>
          {scope.label && (
            <div className={`${GRID} px-1 pt-1.5`}>
              <span className={`truncate pl-8 text-xs ${scope.label.includes(" ") ? "text-foreground/80" : "font-mono tabular-nums text-foreground/80"}`}>{scope.label}</span>
              <span /><span /><span />
            </div>
          )}
          {permissionCredentials(scope).map((cred) => (
            <div key={cred.id} className={`${GRID} px-1 py-0.5 border-b border-border/30`}>
              <span className="truncate pl-12 text-xs text-foreground/70">{cred.label}</span>
              {ROLES.map((role) => (
                <AccessCheck
                  key={role.id}
                  checked={credentialChecked(connector, scope, role.id, cred.id)}
                  disabled={disabled}
                  label={`${role.label} ${cred.label} on ${connector.name} ${scope.label || ""}`.trim()}
                  onChange={(on) => saveCells([{
                    scopeId: scope.id,
                    roleId: role.id,
                    credential: nextCredential(scope, grantedCredential(connector, scope.id, role.id), cred.id, on),
                  }])}
                />
              ))}
            </div>
          ))}
        </Fragment>
      ))}

      {perAccount && connector.scopes.map((scope) => (
        <div key={scope.id || "all"} className={`${GRID} px-1 py-0.5 border-b border-border/30`}>
          <span className="truncate pl-8 text-xs text-muted-foreground">{scope.label || "All"}</span>
          {ROLES.map((role) => (
            <AccessCheck
              key={role.id}
              checked={isChecked(connector, scope.id, role.id)}
              disabled={disabled}
              label={`${role.label} can use ${connector.name} ${scope.label || "account"}`}
              onChange={(on) => saveCells([{
                scopeId: scope.id,
                roleId: role.id,
                credential: on ? preferredCredential(scope) : null,
              }])}
            />
          ))}
        </div>
      ))}
    </>
  );
}

function LockedOn() {
  return (
    <span className="flex h-8 items-center justify-center">
      <Check className="h-3.5 w-3.5 text-foreground/50" />
    </span>
  );
}

function ViewerRunTable({
  run, disabled, onChange,
}: {
  run: ViewerRun;
  disabled: boolean;
  onChange: (next: ViewerRun) => void;
}) {
  return (
    <div className="text-sm">
      <p className="mb-3 text-sm text-muted-foreground">
        Editors and admins can always start a conversation, in Aurora or from Slack. Check Viewer to let that role start one too.
      </p>
      <div className={`${GRID} px-1 pb-2 text-xs font-medium text-muted-foreground border-b border-border`}>
        <span />
        {ROLES.map((role) => (
          <span key={role.id} className="text-center whitespace-nowrap">{role.label}</span>
        ))}
      </div>
      <div className={`${GRID} px-1 py-1.5 border-b border-border/30`}>
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md border border-border/40 bg-muted">
            <MessageSquare className="h-3.5 w-3.5 text-foreground" />
          </div>
          <span className="font-medium">Chats</span>
        </div>
        <AccessCheck
          checked={run.chat}
          disabled={disabled}
          label="Viewers can run chats"
          onChange={(on) => onChange({ chat: on })}
        />
        <LockedOn />
        <LockedOn />
      </div>
    </div>
  );
}

export default function ConnectorAccess() {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [teams, setTeams] = useState<AudienceTeam[]>([]);
  const [audience, setAudience] = useState(EVERYONE);
  const [viewerRun, setViewerRun] = useState<ViewerRun>({ chat: false });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [savingRun, setSavingRun] = useState(false);
  const audienceRef = useRef(audience);
  audienceRef.current = audience;

  const load = useCallback(async (isCurrent: () => boolean = () => true) => {
    const requested = audience;
    // A save-failure reload has no effect callback, so the audience is the other check.
    const still = () => isCurrent() && audienceRef.current === requested;
    const teamQuery = audience === EVERYONE ? "" : `?teamId=${encodeURIComponent(audience)}`;
    try {
      const [accessRes, runRes, teamsRes] = await Promise.all([
        fetch(`/api/org/connector-access${teamQuery}`),
        fetch("/api/org/viewer-run"),
        fetch("/api/org/teams"),
      ]);
      // A slower response for the previous audience must not overwrite this one.
      if (!still()) return;
      if (!accessRes.ok) throw new Error("Could not load connector access");
      const data = await accessRes.json();
      if (!still()) return;
      setConnectors(data.connectors ?? []);
      if (runRes.ok) {
        const run = await runRes.json();
        if (!still()) return;
        setViewerRun({ chat: Boolean(run.chat) });
      }
      if (teamsRes.ok) {
        const teamData = await teamsRes.json();
        if (!still()) return;
        setTeams((teamData.teams ?? []).map((team: AudienceTeam) => ({ id: team.id, name: team.name })));
      }
    } catch {
      if (still()) toast({ title: "Could not load connector access", variant: "destructive" });
    } finally {
      if (still()) setLoading(false);
    }
  }, [audience]);

  useEffect(() => {
    let active = true;
    void load(() => active);
    return () => { active = false; };
  }, [load]);

  async function applyRole(roleId: string, on: boolean) {
    const role = ROLES.find((item) => item.id === roleId);
    const changes = connectors.flatMap((connector) => {
      const next = withCells(connector, connector.scopes.map((scope) => ({
        scopeId: scope.id,
        roleId,
        credential: on ? preferredCredential(scope) : null,
      })));
      // Already in that state — skip the write
      if (connector.limited === next.limited && grantsEqual(connector.grants, next.grants)) return [];
      return [{ id: connector.id, name: connector.name, ...next }];
    });
    if (changes.length === 0) return;
    setSaving("batch");
    const body = JSON.stringify({
      teamId: audience === EVERYONE ? "" : audience,
      updates: changes.map((change) => ({
        connector: change.id,
        limited: change.limited,
        grants: change.grants,
      })),
    });
    try {
      let saved: { connectors?: Connector[] } | null = null;
      for (let attempt = 0; attempt < 2 && !saved; attempt++) {
        const res = await fetch("/api/org/connector-access", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body,
        });
        if (res.ok) {
          saved = await res.json();
          continue;
        }
        // A rejected payload will fail the same way again
        if (res.status < 500) break;
      }
      if (!saved?.connectors) throw new Error("save failed");
      setConnectors(saved.connectors);
    } catch {
      toast({ title: `Could not update ${role?.label ?? "role"} access`, variant: "destructive" });
      await load();
    } finally {
      setSaving(null);
    }
  }

  async function save(connector: Connector, limited: boolean, grants: Grant[]) {
    setSaving(connector.id);
    setConnectors((prev) => prev.map((c) => c.id === connector.id ? { ...c, limited, grants } : c));
    try {
      const res = await fetch(`/api/org/connector-access/${encodeURIComponent(connector.id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limited, grants, teamId: audience === EVERYONE ? "" : audience }),
      });
      if (!res.ok) throw new Error("save failed");
      const updated = await res.json();
      setConnectors((prev) => prev.map((c) => c.id === connector.id ? updated : c));
    } catch {
      toast({ title: `Could not update ${connector.name}`, variant: "destructive" });
      await load();
    } finally {
      setSaving(null);
    }
  }

  async function saveRun(next: ViewerRun) {
    setSavingRun(true);
    setViewerRun(next);
    try {
      const res = await fetch("/api/org/viewer-run", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error("save failed");
    } catch {
      toast({ title: "Could not update viewer access", variant: "destructive" });
      await load();
    } finally {
      setSavingRun(false);
    }
  }

  const teamName = teams.find((team) => team.id === audience)?.name;
  const groups = groupConnectors(connectors);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {audience === EVERYONE
            ? "These rules apply to everyone. A team can override them."
            : `${teamName ?? "This team"} overrides everyone. A connector you have not changed still follows everyone.`}
        </p>
        <Select
          value={audience}
          onValueChange={(value) => {
            setAudience(value);
            setLoading(true);
          }}
        >
          <SelectTrigger className="h-8 w-44 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={EVERYONE} className="text-xs">Everyone</SelectItem>
            {teams.map((team) => (
              <SelectItem key={team.id} value={team.id} className="text-xs">{team.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading connectors…
        </div>
      ) : connectors.length === 0 ? (
        <>
          {audience === EVERYONE && <ViewerRunTable run={viewerRun} disabled={savingRun} onChange={saveRun} />}
          <p className="text-sm text-muted-foreground">No connectors are connected yet.</p>
        </>
      ) : (
      <>
      {audience === EVERYONE && <ViewerRunTable run={viewerRun} disabled={savingRun} onChange={saveRun} />}
      <p className="text-sm text-muted-foreground">
        Check a role to let it use a connector. On AWS and GCP, uncheck Modify to leave read-only, or uncheck Read-only to remove access.
      </p>

      <div className="overflow-x-auto text-sm">
        <div className="min-w-[22rem]">
          <div className={`${GRID} px-1 pb-2 text-xs font-medium text-muted-foreground border-b border-border`}>
            <span>Connector</span>
            {ROLES.map((role) => (
              <div key={role.id} className="flex flex-col items-center gap-0.5 whitespace-nowrap">
                <span>{role.label}</span>
                <span className="flex gap-1.5 text-[10px] font-normal">
                  <button
                    type="button"
                    className="hover:text-foreground disabled:opacity-40"
                    disabled={saving !== null}
                    title={`Select all for ${role.label}`}
                    onClick={() => void applyRole(role.id, true)}
                  >
                    All
                  </button>
                  <button
                    type="button"
                    className="hover:text-foreground disabled:opacity-40"
                    disabled={saving !== null}
                    title={`Unselect all for ${role.label}`}
                    onClick={() => void applyRole(role.id, false)}
                  >
                    None
                  </button>
                </span>
              </div>
            ))}
          </div>

          {groups.map(([category, items]) => (
            <Fragment key={category}>
              <div className="pt-3 pb-1 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/60">
                {category}
              </div>
              {items.map((connector) => (
                <ConnectorRows
                  key={connector.id}
                  connector={connector}
                  disabled={saving !== null}
                  onSave={(limited, grants) => save(connector, limited, grants)}
                />
              ))}
            </Fragment>
          ))}
        </div>
      </div>
      </>
      )}
    </div>
  );
}

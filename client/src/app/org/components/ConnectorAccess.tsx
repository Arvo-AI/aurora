"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/hooks/use-toast";
import { ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, type UserRole } from "@/lib/roles";

const ROLES: { id: UserRole; label: string }[] = [
  { id: ROLE_VIEWER, label: "Viewer" },
  { id: ROLE_EDITOR, label: "Editor" },
  { id: ROLE_ADMIN, label: "Admin" },
];

const NONE = "none";

interface Credential { id: string; label: string }
interface Scope { id: string; label: string; credentials: Credential[] }
interface Grant { scopeId: string; credential: string; role: string }
interface Connector {
  id: string;
  name: string;
  kind: string;
  limited: boolean;
  scopes: Scope[];
  grants: Grant[];
}

function isFlat(connector: Connector): boolean {
  return connector.scopes.length === 1
    && connector.scopes[0].credentials.length === 1
    && connector.scopes[0].credentials[0].id === "";
}

function grantFor(grants: Grant[], scopeId: string, role: string): Grant | undefined {
  return grants.find((g) => g.scopeId === scopeId && g.role === role);
}

/** First time a connector is limited, suggest modify for admins and read-only for viewers. */
function defaultGrants(connector: Connector): Grant[] {
  if (isFlat(connector)) {
    return ROLES.map((role) => ({ scopeId: "", credential: "", role: role.id }));
  }
  const grants: Grant[] = [];
  for (const scope of connector.scopes) {
    const ids = new Set(scope.credentials.map((c) => c.id));
    for (const role of ROLES) {
      const prefer = role.id === ROLE_VIEWER && ids.has("read") ? "read" : ids.has("write") ? "write" : scope.credentials[0]?.id;
      if (prefer !== undefined) grants.push({ scopeId: scope.id, credential: prefer, role: role.id });
    }
  }
  return grants;
}

export default function ConnectorAccess() {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [loading, setLoading] = useState(true);
  const [byRole, setByRole] = useState(false);
  const [role, setRole] = useState<UserRole>(ROLE_VIEWER);
  const [saving, setSaving] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/org/connector-access");
      if (!res.ok) throw new Error("Could not load connector access");
      const data = await res.json();
      setConnectors(data.connectors ?? []);
    } catch {
      toast({ title: "Could not load connector access", variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function save(connector: Connector, limited: boolean, grants: Grant[]) {
    setSaving(connector.id);
    setConnectors((prev) => prev.map((c) => c.id === connector.id ? { ...c, limited, grants } : c));
    try {
      const res = await fetch(`/api/org/connector-access/${encodeURIComponent(connector.id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limited, grants }),
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

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading connectors…
      </div>
    );
  }

  if (connectors.length === 0) {
    return <p className="text-sm text-muted-foreground">No connectors are connected yet.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="max-w-xl text-sm text-muted-foreground">
          Choose which role can use each connector. For AWS and GCP you can also
          pick the account or project, and whether that role gets the modify
          identity or the read-only one. Connectors you leave open stay available
          to everyone.
        </p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className={`text-sm ${byRole ? "text-muted-foreground" : "font-medium"}`}
            onClick={() => setByRole(false)}
          >
            By connector
          </button>
          <Switch checked={byRole} onCheckedChange={setByRole} />
          <button
            type="button"
            className={`text-sm ${byRole ? "font-medium" : "text-muted-foreground"}`}
            onClick={() => setByRole(true)}
          >
            By role
          </button>
        </div>
      </div>

      {byRole && (
        <Select value={role} onValueChange={(value) => setRole(value as UserRole)}>
          <SelectTrigger className="w-[180px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ROLES.map((item) => (
              <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}

      <ul className="space-y-3">
        {connectors.map((connector) => (
          <li key={connector.id} className="rounded-md border p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="font-medium">{connector.name}</div>
                {connector.kind === "mcp" && (
                  <div className="text-xs text-muted-foreground">Custom MCP</div>
                )}
              </div>
              <label className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">Limit access</span>
                <Switch
                  checked={connector.limited}
                  disabled={saving === connector.id}
                  onCheckedChange={(on) => save(
                    connector,
                    on,
                    on ? defaultGrants(connector) : [],
                  )}
                />
              </label>
            </div>
            {connector.limited && (
              <div className="mt-4">
                {isFlat(connector) ? (
                  <FlatGrants
                    connector={connector}
                    onlyRole={byRole ? role : undefined}
                    disabled={saving === connector.id}
                    onChange={(grants) => save(connector, true, grants)}
                  />
                ) : (
                  <ScopedGrants
                    connector={connector}
                    onlyRole={byRole ? role : undefined}
                    disabled={saving === connector.id}
                    onChange={(grants) => save(connector, true, grants)}
                  />
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function FlatGrants({
  connector, onlyRole, disabled, onChange,
}: {
  connector: Connector;
  onlyRole?: UserRole;
  disabled: boolean;
  onChange: (grants: Grant[]) => void;
}) {
  const roles = onlyRole ? ROLES.filter((r) => r.id === onlyRole) : ROLES;
  return (
    <div className="flex flex-wrap gap-4">
      {roles.map((item) => {
        const checked = connector.grants.some((g) => g.role === item.id);
        return (
          <label key={item.id} className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={checked}
              disabled={disabled}
              onCheckedChange={(on) => {
                const grants = connector.grants.filter((g) => g.role !== item.id);
                if (on) grants.push({ scopeId: "", credential: "", role: item.id });
                onChange(grants);
              }}
            />
            {item.label}
          </label>
        );
      })}
    </div>
  );
}

function ScopedGrants({
  connector, onlyRole, disabled, onChange,
}: {
  connector: Connector;
  onlyRole?: UserRole;
  disabled: boolean;
  onChange: (grants: Grant[]) => void;
}) {
  const roles = onlyRole ? ROLES.filter((r) => r.id === onlyRole) : ROLES;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th className="pb-2 pr-3 font-medium">Account</th>
            {roles.map((item) => (
              <th key={item.id} className="pb-2 pr-3 font-medium">{item.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {connector.scopes.map((scope) => (
            <tr key={scope.id || "all"}>
              <td className="py-1.5 pr-3 font-mono text-xs">{scope.label || "All"}</td>
              {roles.map((item) => {
                const current = grantFor(connector.grants, scope.id, item.id);
                // Radix select rejects an empty value, so the single-credential
                // case (no modify/read split) is stored as "" and shown as "access".
                const shown = current ? (current.credential || "access") : NONE;
                return (
                  <td key={item.id} className="py-1.5 pr-3">
                    <Select
                      value={shown}
                      disabled={disabled}
                      onValueChange={(value) => {
                        const grants = connector.grants.filter(
                          (g) => !(g.scopeId === scope.id && g.role === item.id),
                        );
                        if (value !== NONE) {
                          grants.push({
                            scopeId: scope.id,
                            credential: value === "access" ? "" : value,
                            role: item.id,
                          });
                        }
                        onChange(grants);
                      }}
                    >
                      <SelectTrigger className="h-8 w-[200px] text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NONE}>No access</SelectItem>
                        {scope.credentials.map((cred) => (
                          <SelectItem key={cred.id || "access"} value={cred.id || "access"}>
                            {cred.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

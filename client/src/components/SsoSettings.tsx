"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useToast } from "@/components/ui/use-toast";
import { Copy, Loader2, Trash2 } from "lucide-react";

type SsoDomain = {
  id: string;
  domain: string;
  verified: boolean;
  dnsRecord: { name: string; value: string };
};

type SsoConfig = {
  idpEntityId: string;
  idpSsoUrl: string;
  idpX509Cert: string;
  defaultRole: string;
  enabled: boolean;
  requireSso: boolean;
};

type SsoSettingsResponse = {
  config: SsoConfig | null;
  domains: SsoDomain[];
  serviceProvider: { entityId: string; acsUrl: string; loginUrl: string; metadataUrl: string };
  domainVerificationRequired: boolean;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data as T;
}

function CopyField({ label, value }: Readonly<{ label: string; value: string }>) {
  const { toast } = useToast();
  return (
    <div className="space-y-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <div className="flex gap-2">
        <Input readOnly value={value} className="font-mono text-xs h-8" />
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-8 p-0 shrink-0"
          aria-label={`Copy ${label}`}
          onClick={() => {
            navigator.clipboard.writeText(value).then(
              () => toast({ title: "Copied", description: label }),
              () => toast({ title: "Couldn't copy", description: "Select the text and copy it manually.", variant: "destructive" }),
            );
          }}
        >
          <Copy className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}

export function SsoSettings() {
  const { toast } = useToast();
  const [data, setData] = useState<SsoSettingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyDomain, setBusyDomain] = useState<string | null>(null);
  const [newDomain, setNewDomain] = useState("");

  const [metadataXml, setMetadataXml] = useState("");
  const [entityId, setEntityId] = useState("");
  const [ssoUrl, setSsoUrl] = useState("");
  const [cert, setCert] = useState("");
  const [defaultRole, setDefaultRole] = useState("viewer");
  const [enabled, setEnabled] = useState(false);
  const [requireSso, setRequireSso] = useState(false);

  const applyResponse = useCallback((res: SsoSettingsResponse) => {
    setData(res);
    if (res.config) {
      setEntityId(res.config.idpEntityId);
      setSsoUrl(res.config.idpSsoUrl);
      setCert(res.config.idpX509Cert);
      setDefaultRole(res.config.defaultRole);
      setEnabled(res.config.enabled);
      setRequireSso(res.config.requireSso);
    }
  }, []);

  useEffect(() => {
    api<SsoSettingsResponse>("/api/orgs/sso")
      .then(applyResponse)
      .catch((e: Error) => toast({ title: "Failed to load SSO settings", description: e.message, variant: "destructive" }))
      .finally(() => setLoading(false));
  }, [applyResponse, toast]);

  const save = async () => {
    setSaving(true);
    try {
      // Pasted metadata wins; otherwise send the individual fields
      const idp = metadataXml.trim()
        ? { idpMetadataXml: metadataXml }
        : { idpEntityId: entityId, idpSsoUrl: ssoUrl, idpX509Cert: cert };
      const res = await api<SsoSettingsResponse>("/api/orgs/sso", {
        method: "PUT",
        body: JSON.stringify({ ...idp, defaultRole, enabled, requireSso: enabled && requireSso }),
      });
      applyResponse(res);
      setMetadataXml("");
      toast({ title: "SSO settings saved" });
    } catch (e) {
      toast({ title: "Couldn't save SSO settings", description: (e as Error).message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  const domainAction = async (key: string, path: string, init: RequestInit, success: string) => {
    setBusyDomain(key);
    try {
      const res = await api<{ domains: SsoDomain[] }>(path, init);
      setData((prev) => (prev ? { ...prev, domains: res.domains } : prev));
      toast({ title: success });
      return true;
    } catch (e) {
      toast({ title: "Domain update failed", description: (e as Error).message, variant: "destructive" });
      return false;
    } finally {
      setBusyDomain(null);
    }
  };

  if (loading) {
    return <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />Loading...</div>;
  }
  if (!data) return null;

  const sp = data.serviceProvider;
  const hasVerifiedDomain = data.domains.some((d) => d.verified);

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h2 className="text-2xl font-bold">Single Sign-On (SAML)</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Let members sign in with your identity provider, such as Microsoft Entra ID, Okta or Google Workspace.
        </p>
      </div>

      <Card>
        <CardContent className="pt-6 space-y-4">
          <div>
            <h3 className="font-semibold">1. Register Aurora in your identity provider</h3>
            <p className="text-xs text-muted-foreground mt-1">
              Create a SAML application and paste these values, or import the metadata URL.
            </p>
          </div>
          <CopyField label="Identifier (Entity ID)" value={sp.entityId} />
          <CopyField label="Reply URL (Assertion Consumer Service URL)" value={sp.acsUrl} />
          <CopyField label="Sign on URL" value={sp.loginUrl} />
          <CopyField label="Metadata URL" value={sp.metadataUrl} />
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-6 space-y-4">
          <div>
            <h3 className="font-semibold">2. Verify your email domains</h3>
            <p className="text-xs text-muted-foreground mt-1">
              Members whose email is on a verified domain are routed to your identity provider.
              {data.domainVerificationRequired && " Prove ownership by adding a DNS TXT record."}
            </p>
          </div>
          <form
            className="flex gap-2"
            onSubmit={async (e) => {
              e.preventDefault();
              const ok = await domainAction("new", "/api/orgs/sso/domains", { method: "POST", body: JSON.stringify({ domain: newDomain }) }, "Domain added");
              if (ok) setNewDomain("");
            }}
          >
            <Input value={newDomain} onChange={(e) => setNewDomain(e.target.value)} placeholder="example.com" className="h-8 text-sm" />
            <Button type="submit" size="sm" className="h-8" disabled={!newDomain.trim() || busyDomain === "new"}>Add domain</Button>
          </form>
          {data.domains.length === 0 && <p className="text-xs text-muted-foreground">No domains yet.</p>}
          <div className="divide-y divide-border rounded-md border">
            {data.domains.map((d) => (
              <div key={d.id} className="p-3 space-y-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm flex-1">{d.domain}</span>
                  {d.verified ? <Badge>Verified</Badge> : <Badge variant="outline">Pending</Badge>}
                  {!d.verified && (
                    <Button size="sm" variant="outline" className="h-7 text-xs" disabled={busyDomain === d.id}
                      onClick={() => domainAction(d.id, `/api/orgs/sso/domains/${d.id}/verify`, { method: "POST" }, "Domain verified")}>
                      {busyDomain === d.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Verify"}
                    </Button>
                  )}
                  <Button size="sm" variant="ghost" className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive" aria-label={`Remove ${d.domain}`}
                    disabled={busyDomain === d.id}
                    onClick={() => domainAction(d.id, `/api/orgs/sso/domains/${d.id}`, { method: "DELETE" }, "Domain removed")}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
                {!d.verified && (
                  <div className="grid gap-2 sm:grid-cols-2">
                    <CopyField label="TXT record name" value={d.dnsRecord.name} />
                    <CopyField label="TXT record value" value={d.dnsRecord.value} />
                  </div>
                )}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-6 space-y-4">
          <div>
            <h3 className="font-semibold">3. Connect your identity provider</h3>
            <p className="text-xs text-muted-foreground mt-1">
              Paste the federation metadata XML from your identity provider, or fill in the fields below.
            </p>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">Federation metadata XML</Label>
            <Textarea value={metadataXml} onChange={(e) => setMetadataXml(e.target.value)} rows={4}
              placeholder="<EntityDescriptor ...>" className="font-mono text-xs" />
          </div>
          <div className="grid gap-3">
            <div className="space-y-1">
              <Label className="text-xs">IdP Entity ID (Issuer)</Label>
              <Input value={entityId} onChange={(e) => setEntityId(e.target.value)} disabled={!!metadataXml.trim()} className="h-8 text-xs font-mono" />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">IdP SSO URL (Login URL)</Label>
              <Input value={ssoUrl} onChange={(e) => setSsoUrl(e.target.value)} disabled={!!metadataXml.trim()} className="h-8 text-xs font-mono" />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Signing certificate (PEM or Base64)</Label>
              <Textarea value={cert} onChange={(e) => setCert(e.target.value)} disabled={!!metadataXml.trim()} rows={3} className="font-mono text-xs" />
            </div>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">Role for new members</Label>
            <Select value={defaultRole} onValueChange={setDefaultRole}>
              <SelectTrigger className="h-8 w-40 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="viewer">Viewer</SelectItem>
                <SelectItem value="editor">Editor</SelectItem>
                <SelectItem value="admin">Admin</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center justify-between rounded-md border p-3">
            <div>
              <p className="text-sm font-medium">Enable SSO</p>
              <p className="text-xs text-muted-foreground">
                {hasVerifiedDomain ? "Members can sign in with SSO." : "Verify at least one domain so members can be routed to SSO."}
              </p>
            </div>
            <Switch checked={enabled} onCheckedChange={(v) => { setEnabled(v); if (!v) setRequireSso(false); }} aria-label="Enable SSO" />
          </div>
          <div className="flex items-center justify-between rounded-md border p-3">
            <div>
              <p className="text-sm font-medium">Require SSO</p>
              <p className="text-xs text-muted-foreground">
                Block password sign-in for members on verified domains. Admins keep password access in case the identity provider is unavailable.
              </p>
            </div>
            <Switch checked={requireSso} disabled={!enabled} onCheckedChange={setRequireSso} aria-label="Require SSO" />
          </div>
          <div className="flex justify-end">
            <Button onClick={save} disabled={saving}>
              {saving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}Save
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

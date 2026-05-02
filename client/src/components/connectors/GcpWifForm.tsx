"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertCircle, Check, Loader2, ShieldCheck } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";
import { ProjectCache } from "@/components/cloud-provider/projects/projectUtils";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";

interface GcpWifFormProps {
  onSuccess: () => void;
}

export function GcpWifForm({ onSuccess }: GcpWifFormProps) {
  const { toast } = useToast();
  const [loading, setLoading] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const copyText = (text: string, id: string) => {
    void navigator.clipboard.writeText(text);
    setCopied(id);
    setTimeout(() => setCopied((v) => (v === id ? null : v)), 1500);
  };

  const [projectId, setProjectId] = useState("");
  const [projectNumber, setProjectNumber] = useState("");
  const [poolId, setPoolId] = useState("aurora-wif-pool");
  const [providerId, setProviderId] = useState("aurora-provider");
  const [saEmail, setSaEmail] = useState("");
  const [viewerSaEmail, setViewerSaEmail] = useState("");
  const [orgId, setOrgId] = useState("");
  const [additionalProjects, setAdditionalProjects] = useState("");

  const isValid = projectId.trim() && projectNumber.trim() && saEmail.trim();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isValid || loading) return;

    setLoading(true);
    setSubmitError(null);

    try {
      const body: Record<string, unknown> = {
        project_id: projectId.trim(),
        project_number: projectNumber.trim(),
        pool_id: poolId.trim(),
        provider_id: providerId.trim(),
        sa_email: saEmail.trim(),
      };
      if (viewerSaEmail.trim()) {
        body.viewer_sa_email = viewerSaEmail.trim();
      }
      if (orgId.trim()) {
        body.org_id = orgId.trim();
      }
      const extra = additionalProjects
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (extra.length > 0) {
        body.additional_project_ids = extra;
      }

      const response = await fetch("/api/gcp/wif/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });

      const payload = await response.json().catch(() => null);

      if (!response.ok) {
        const msg =
          payload &&
          typeof payload === "object" &&
          "error" in payload &&
          typeof payload.error === "string"
            ? payload.error
            : "WIF connection failed";
        setSubmitError(msg);
        toast({ title: "Failed to connect", description: msg, variant: "destructive" });
        return;
      }

      const email = payload?.email ?? saEmail;
      const count = Array.isArray(payload?.accessible_projects)
        ? payload.accessible_projects.length
        : null;
      toast({
        title: "GCP connected via WIF",
        description: count !== null
          ? `Connected as ${email} - ${count} project${count === 1 ? "" : "s"} accessible.`
          : `Connected as ${email}.`,
      });

      ProjectCache.invalidate("gcp");
      void fetchConnectedAccounts(true).catch(() => {});
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
      onSuccess();
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : "Connection failed";
      setSubmitError(msg);
      toast({ title: "Failed to connect", description: msg, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const pid = projectId.trim() || "<YOUR_PROJECT_ID>";
  const pnum = projectNumber.trim() || "<YOUR_PROJECT_NUMBER>";
  const pool = poolId.trim() || "aurora-wif-pool";
  const provider = providerId.trim() || "aurora-provider";
  const agentSa = saEmail.trim()
    ? saEmail.trim()
    : projectId.trim()
      ? `aurora-agent@${projectId.trim()}.iam.gserviceaccount.com`
      : "aurora-agent@<PROJECT_ID>.iam.gserviceaccount.com";
  const viewerSa = viewerSaEmail.trim()
    ? viewerSaEmail.trim()
    : projectId.trim()
      ? `aurora-viewer@${projectId.trim()}.iam.gserviceaccount.com`
      : "aurora-viewer@<PROJECT_ID>.iam.gserviceaccount.com";
  const org = orgId.trim();

  const iamBindCmd = org
    ? `# Org-level IAM (applies to all projects in org ${org})
gcloud organizations add-iam-policy-binding ${org} \\
  --member="serviceAccount:${agentSa}" \\
  --role="roles/editor" --condition=None --quiet
gcloud organizations add-iam-policy-binding ${org} \\
  --member="serviceAccount:${agentSa}" \\
  --role="roles/iam.serviceAccountUser" --condition=None --quiet
gcloud organizations add-iam-policy-binding ${org} \\
  --member="serviceAccount:${agentSa}" \\
  --role="roles/resourcemanager.organizationViewer" --condition=None --quiet

gcloud organizations add-iam-policy-binding ${org} \\
  --member="serviceAccount:${viewerSa}" \\
  --role="roles/viewer" --condition=None --quiet`
    : `# Project-level IAM
gcloud projects add-iam-policy-binding ${pid} \\
  --member="serviceAccount:${agentSa}" \\
  --role="roles/editor" --condition=None --quiet
gcloud projects add-iam-policy-binding ${pid} \\
  --member="serviceAccount:${agentSa}" \\
  --role="roles/iam.serviceAccountUser" --condition=None --quiet

gcloud projects add-iam-policy-binding ${pid} \\
  --member="serviceAccount:${viewerSa}" \\
  --role="roles/viewer" --condition=None --quiet`;

  const gcloudScript = `#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${pid}"
PROJECT_NUMBER="${pnum}"
POOL_ID="${pool}"
PROVIDER_ID="${provider}"
AURORA_ISSUER="<AURORA_OIDC_ISSUER>"
AURORA_SA="<AURORA_SA_EMAIL>"

# Enable required APIs
gcloud services enable \\
  sts.googleapis.com iamcredentials.googleapis.com iam.googleapis.com \\
  cloudresourcemanager.googleapis.com compute.googleapis.com \\
  container.googleapis.com storage.googleapis.com monitoring.googleapis.com \\
  logging.googleapis.com bigquery.googleapis.com cloudasset.googleapis.com \\
  --project="$PROJECT_ID" --quiet

# Create WIF pool + provider
gcloud iam workload-identity-pools create "$POOL_ID" \\
  --project="$PROJECT_ID" --location=global \\
  --display-name="Aurora WIF Pool" 2>/dev/null || true

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \\
  --project="$PROJECT_ID" --location=global \\
  --workload-identity-pool="$POOL_ID" \\
  --issuer-uri="$AURORA_ISSUER" \\
  --attribute-mapping="google.subject=assertion.sub" \\
  --attribute-condition="google.subject == \\"$AURORA_SA\\"" 2>/dev/null || true

# Create service accounts
gcloud iam service-accounts create aurora-agent \\
  --project="$PROJECT_ID" --display-name="Aurora Agent" 2>/dev/null || true
gcloud iam service-accounts create aurora-viewer \\
  --project="$PROJECT_ID" --display-name="Aurora Viewer" 2>/dev/null || true

# Grant WIF federation on both SAs
POOL_RESOURCE="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID"
for SA in aurora-agent aurora-viewer; do
  gcloud iam service-accounts add-iam-policy-binding \\
    "$SA@$PROJECT_ID.iam.gserviceaccount.com" \\
    --project="$PROJECT_ID" \\
    --role="roles/iam.workloadIdentityUser" \\
    --member="principalSet://iam.googleapis.com/$POOL_RESOURCE/*" \\
    --condition=None --quiet
done

${iamBindCmd}`;

  const terraformBlock = `module "aurora" {
  source            = "./aurora-wif"
  project_id        = "${pid}"
  aurora_oidc_issuer = "<AURORA_OIDC_ISSUER>"
  aurora_sa_email   = "<AURORA_SA_EMAIL>"${org ? `\n  org_id            = "${org}"` : ""}
}`;

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2 text-sm text-muted-foreground">
        <p>
          Run the gcloud commands or Terraform module in your GCP project,
          then fill in the values below. Aurora verifies access instantly.
        </p>
      </div>

      <Collapsible open={setupOpen} onOpenChange={setSetupOpen}>
        <CollapsibleTrigger asChild>
          <Button variant="outline" size="sm" type="button" className="w-full">
            {setupOpen ? "Hide" : "Show"} setup instructions
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent className="mt-3 space-y-3 text-xs text-muted-foreground rounded-lg border p-3">
          <div>
            <div className="flex items-center justify-between mb-1">
              <p className="font-medium text-foreground">Option A: gcloud</p>
              <Button
                type="button" variant="ghost" size="sm" className="h-6 text-xs px-2"
                onClick={() => copyText(gcloudScript, "gcloud")}
              >
                {copied === "gcloud" ? <><Check className="h-3 w-3 mr-1" />Copied</> : "Copy"}
              </Button>
            </div>
            <pre className="bg-muted p-2 rounded text-xs overflow-x-auto whitespace-pre max-h-72 overflow-y-auto">
              {gcloudScript}
            </pre>
          </div>
          <div>
            <div className="flex items-center justify-between mb-1">
              <p className="font-medium text-foreground">Option B: Terraform</p>
              <Button
                type="button" variant="ghost" size="sm" className="h-6 text-xs px-2"
                onClick={() => copyText(terraformBlock, "terraform")}
              >
                {copied === "terraform" ? <><Check className="h-3 w-3 mr-1" />Copied</> : "Copy"}
              </Button>
            </div>
            <pre className="bg-muted p-2 rounded text-xs overflow-x-auto">
              {terraformBlock}
            </pre>
          </div>
        </CollapsibleContent>
      </Collapsible>

      <div className="grid gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="wif-project-id" className="text-sm">Project ID</Label>
          <Input
            id="wif-project-id"
            value={projectId}
            onChange={(e) => { setProjectId(e.target.value); setSubmitError(null); }}
            placeholder="my-gcp-project"
            disabled={loading}
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="wif-project-number" className="text-sm">Project Number</Label>
          <Input
            id="wif-project-number"
            value={projectNumber}
            onChange={(e) => { setProjectNumber(e.target.value); setSubmitError(null); }}
            placeholder="123456789"
            disabled={loading}
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="wif-pool-id" className="text-sm">Pool ID</Label>
            <Input
              id="wif-pool-id"
              value={poolId}
              onChange={(e) => setPoolId(e.target.value)}
              disabled={loading}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="wif-provider-id" className="text-sm">Provider ID</Label>
            <Input
              id="wif-provider-id"
              value={providerId}
              onChange={(e) => setProviderId(e.target.value)}
              disabled={loading}
            />
          </div>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="wif-sa-email" className="text-sm">Agent SA Email</Label>
          <Input
            id="wif-sa-email"
            value={saEmail}
            onChange={(e) => { setSaEmail(e.target.value); setSubmitError(null); }}
            placeholder="aurora-agent@my-project.iam.gserviceaccount.com"
            disabled={loading}
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="wif-viewer-sa" className="text-sm">
            Viewer SA Email <span className="text-muted-foreground">(optional)</span>
          </Label>
          <Input
            id="wif-viewer-sa"
            value={viewerSaEmail}
            onChange={(e) => setViewerSaEmail(e.target.value)}
            placeholder="aurora-viewer@my-project.iam.gserviceaccount.com"
            disabled={loading}
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="wif-org-id" className="text-sm">
            Organization ID <span className="text-muted-foreground">(optional - grants access to all org projects)</span>
          </Label>
          <Input
            id="wif-org-id"
            value={orgId}
            onChange={(e) => setOrgId(e.target.value)}
            placeholder="123456789012"
            disabled={loading}
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="wif-additional" className="text-sm">
            Additional Project IDs <span className="text-muted-foreground">(comma-separated, optional)</span>
          </Label>
          <Input
            id="wif-additional"
            value={additionalProjects}
            onChange={(e) => setAdditionalProjects(e.target.value)}
            placeholder="project-2, project-3"
            disabled={loading}
          />
        </div>
      </div>

      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
        <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">No secrets stored</p>
          <p className="text-muted-foreground">
            WIF uses federated identity. Aurora never stores private keys or
            OAuth refresh tokens for your GCP project.
          </p>
        </div>
      </div>

      {submitError && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Connection failed</AlertTitle>
          <AlertDescription className="text-sm">{submitError}</AlertDescription>
        </Alert>
      )}

      <Button type="submit" disabled={loading || !isValid} className="w-full h-10">
        {loading ? (
          <>
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            Verifying access...
          </>
        ) : (
          "Connect with WIF"
        )}
      </Button>
    </form>
  );
}

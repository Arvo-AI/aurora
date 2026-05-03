"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertCircle, Check, Loader2, ShieldCheck, Info } from "lucide-react";
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
  const [auroraSaEmail, setAuroraSaEmail] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/gcp/wif/setup-info", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setAuroraSaEmail(d?.aurora_sa_email ?? ""))
      .catch(() => setAuroraSaEmail(""));
  }, []);

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

  const wifAvailable = auroraSaEmail !== null && auroraSaEmail !== "";
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
AURORA_SA="${auroraSaEmail}"

# Enable required APIs
gcloud services enable \\
  sts.googleapis.com iamcredentials.googleapis.com iam.googleapis.com \\
  cloudresourcemanager.googleapis.com compute.googleapis.com \\
  container.googleapis.com storage.googleapis.com monitoring.googleapis.com \\
  logging.googleapis.com bigquery.googleapis.com cloudasset.googleapis.com \\
  --project="$PROJECT_ID" --quiet

# Create WIF pool + OIDC provider
gcloud iam workload-identity-pools create "$POOL_ID" \\
  --project="$PROJECT_ID" --location=global \\
  --display-name="Aurora WIF Pool" 2>&1 || echo "(pool may already exist)"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \\
  --project="$PROJECT_ID" --location=global \\
  --workload-identity-pool="$POOL_ID" \\
  --issuer-uri="https://accounts.google.com" \\
  --attribute-mapping="google.subject=assertion.sub,attribute.email=assertion.email" \\
  --attribute-condition="attribute.email == \\"$AURORA_SA\\"" 2>&1 || echo "(provider may already exist)"

# Create service accounts in your project
gcloud iam service-accounts create aurora-agent \\
  --project="$PROJECT_ID" --display-name="Aurora Agent" 2>&1 || echo "(SA may already exist)"
gcloud iam service-accounts create aurora-viewer \\
  --project="$PROJECT_ID" --display-name="Aurora Viewer" 2>&1 || echo "(SA may already exist)"

# Allow the WIF pool to impersonate both service accounts
POOL_RESOURCE="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID"
for SA in aurora-agent aurora-viewer; do
  gcloud iam service-accounts add-iam-policy-binding \\
    "$SA@$PROJECT_ID.iam.gserviceaccount.com" \\
    --project="$PROJECT_ID" \\
    --role="roles/iam.workloadIdentityUser" \\
    --member="principalSet://iam.googleapis.com/$POOL_RESOURCE/*" \\
    --condition=None --quiet
done

${iamBindCmd}

echo ""
echo "===== Setup complete ====="
echo "Fill these into the Aurora form:"
echo "  Agent SA Email:  aurora-agent@$PROJECT_ID.iam.gserviceaccount.com"
echo "  Viewer SA Email: aurora-viewer@$PROJECT_ID.iam.gserviceaccount.com"
echo "  Pool ID:         $POOL_ID"
echo "  Provider ID:     $PROVIDER_ID"
echo "=========================="`;

  const terraformBlock = `module "aurora" {
  source          = "./aurora-wif"
  project_id      = "${pid}"
  aurora_sa_email = "${auroraSaEmail}"${org ? `\n  org_id          = "${org}"` : ""}
}`;

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {auroraSaEmail === null ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground py-4 justify-center">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading WIF configuration...
        </div>
      ) : !wifAvailable ? (
        <Alert>
          <Info className="h-4 w-4" />
          <AlertTitle>WIF not configured</AlertTitle>
          <AlertDescription className="text-sm">
            Workload Identity Federation is not configured on this Aurora instance.
            Ask your Aurora administrator to set the <code className="bg-muted px-1 rounded text-xs">AURORA_WIF_*</code> environment variables.
          </AlertDescription>
        </Alert>
      ) : (
        <>
          <Collapsible open={setupOpen} onOpenChange={setSetupOpen}>
            <CollapsibleTrigger asChild>
              <Button variant="outline" size="sm" type="button" className="w-full">
                {setupOpen ? "Hide" : "Show"} setup instructions
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-3 space-y-4 text-xs text-muted-foreground rounded-lg border p-4">
              <div className="space-y-1">
                <p className="font-medium text-foreground text-sm">Before you begin</p>
                <ul className="list-disc ml-4 space-y-0.5">
                  <li>You need <span className="font-medium text-foreground">Owner</span> access to the GCP project you want to connect.</li>
                  <li>Install the <a href="https://cloud.google.com/sdk/docs/install" target="_blank" rel="noopener noreferrer" className="underline">gcloud CLI</a> and run <code className="bg-muted px-1 rounded">gcloud auth login</code>.</li>
                </ul>
              </div>

              <div className="space-y-1.5">
                <p className="font-medium text-foreground text-sm">Step 1 &mdash; Enter your Project ID and Number</p>
                <p>
                  Scroll down to the form fields and fill in your{" "}
                  <span className="font-medium text-foreground">Project ID</span> and{" "}
                  <span className="font-medium text-foreground">Project Number</span>.
                  The setup scripts below update automatically as you type. Find these values in the{" "}
                  <a href="https://console.cloud.google.com/home/dashboard" target="_blank" rel="noopener noreferrer" className="underline">GCP Console dashboard</a>,
                  or run:
                </p>
                <div className="flex items-center gap-1">
                  <code className="bg-muted px-1.5 py-0.5 rounded flex-1 break-all">gcloud projects describe YOUR_PROJECT_ID --format=&quot;value(projectNumber)&quot;</code>
                  <Button
                    type="button" variant="ghost" size="sm" className="h-6 text-xs px-2 shrink-0"
                    onClick={() => copyText(`gcloud projects describe ${pid} --format="value(projectNumber)"`, "projnum")}
                  >
                    {copied === "projnum" ? <><Check className="h-3 w-3 mr-1" />Copied</> : "Copy"}
                  </Button>
                </div>
              </div>

              <div className="space-y-1.5">
                <p className="font-medium text-foreground text-sm">Step 2 &mdash; Run the setup script</p>
                <p>
                  Copy one of the scripts below and run it in your terminal. The script will:
                </p>
                <ul className="list-disc ml-4 space-y-0.5">
                  <li>Enable the required GCP APIs on your project</li>
                  <li>Create a WIF pool that trusts this Aurora instance</li>
                  <li>Create two service accounts in your project: <code className="bg-muted px-1 rounded">aurora-agent</code> (full access) and <code className="bg-muted px-1 rounded">aurora-viewer</code> (read-only)</li>
                  <li>Grant the service accounts the necessary IAM roles</li>
                </ul>
                <div className="flex items-start gap-2 p-2 rounded bg-blue-500/10 text-xs mt-1.5">
                  <Info className="h-3.5 w-3.5 text-blue-500 shrink-0 mt-0.5" />
                  <p>
                    The <code className="bg-muted px-1 rounded">AURORA_SA</code> value in the
                    script is this Aurora instance&apos;s identity ({auroraSaEmail}). It is pre-filled
                    and should not be changed.
                  </p>
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-1">
                  <p className="font-medium text-foreground">Option A: gcloud CLI</p>
                  <Button
                    type="button" variant="ghost" size="sm" className="h-6 text-xs px-2"
                    onClick={() => copyText(gcloudScript, "gcloud")}
                  >
                    {copied === "gcloud" ? <><Check className="h-3 w-3 mr-1" />Copied</> : "Copy"}
                  </Button>
                </div>
                <p className="mb-1.5">Copy and paste into your terminal. Takes 1-2 minutes.</p>
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
                <p className="mb-1.5">
                  Copy Aurora&apos;s Terraform module files (<code className="bg-muted px-1 rounded">main.tf</code> and{" "}
                  <code className="bg-muted px-1 rounded">variables.tf</code>) into a directory called{" "}
                  <code className="bg-muted px-1 rounded">aurora-wif/</code> next to your Terraform config.
                  These files are available in Aurora&apos;s source at{" "}
                  <code className="bg-muted px-1 rounded">server/connectors/gcp_connector/terraform/</code>.
                  Then add this block and run{" "}
                  <code className="bg-muted px-1 rounded">terraform init && terraform apply</code>.
                  Your Google provider must be configured separately.
                </p>
                <pre className="bg-muted p-2 rounded text-xs overflow-x-auto">
                  {terraformBlock}
                </pre>
              </div>

              <div className="space-y-1.5">
                <p className="font-medium text-foreground text-sm">Step 3 &mdash; Fill in the form and connect</p>
                <p>Once the script completes, fill in the remaining fields below using the output values:</p>
                <ul className="list-disc ml-4 space-y-0.5">
                  <li><span className="font-medium text-foreground">Agent SA Email</span> &rarr; <code className="bg-muted px-1 rounded">aurora-agent@{pid}.iam.gserviceaccount.com</code></li>
                  <li><span className="font-medium text-foreground">Viewer SA Email</span> &rarr; <code className="bg-muted px-1 rounded">aurora-viewer@{pid}.iam.gserviceaccount.com</code></li>
                  <li><span className="font-medium text-foreground">Pool ID</span> and <span className="font-medium text-foreground">Provider ID</span> are pre-filled with defaults</li>
                </ul>
                <p>Then click <span className="font-medium text-foreground">Connect with WIF</span>. Aurora verifies access instantly.</p>
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
        </>
      )}
    </form>
  );
}

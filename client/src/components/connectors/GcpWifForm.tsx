"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertCircle, Loader2, ShieldCheck } from "lucide-react";
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

  const [projectId, setProjectId] = useState("");
  const [projectNumber, setProjectNumber] = useState("");
  const [poolId, setPoolId] = useState("aurora-wif-pool");
  const [providerId, setProviderId] = useState("aurora-provider");
  const [saEmail, setSaEmail] = useState("");
  const [viewerSaEmail, setViewerSaEmail] = useState("");
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

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2 text-sm text-muted-foreground">
        <p>
          Run the Aurora Terraform module or gcloud script in your GCP project,
          then paste the output values below. Aurora verifies access instantly
          with no background tasks.
        </p>
      </div>

      <Collapsible open={setupOpen} onOpenChange={setSetupOpen}>
        <CollapsibleTrigger asChild>
          <Button variant="outline" size="sm" type="button" className="w-full">
            {setupOpen ? "Hide" : "Show"} setup instructions
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent className="mt-3 space-y-2 text-xs text-muted-foreground rounded-lg border p-3">
          <p className="font-medium text-foreground">Option A: Terraform</p>
          <pre className="bg-muted p-2 rounded text-xs overflow-x-auto">
{`module "aurora" {
  source           = "./aurora-wif"
  project_id       = "your-project-id"
  aurora_oidc_issuer = "<from Aurora setup page>"
  aurora_sa_email    = "<from Aurora setup page>"
}`}
          </pre>
          <p className="font-medium text-foreground mt-3">Option B: gcloud script</p>
          <pre className="bg-muted p-2 rounded text-xs overflow-x-auto">
{`bash setup.sh \\
  --project your-project-id \\
  --aurora-issuer <issuer URL> \\
  --aurora-sa <SA email>`}
          </pre>
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

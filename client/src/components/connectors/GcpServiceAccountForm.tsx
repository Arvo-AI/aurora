"use client";

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  AlertCircle,
  Check,
  Loader2,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";
import { ProjectCache } from "@/components/cloud-provider/projects/projectUtils";

interface GcpServiceAccountFormProps {
  onSuccess: () => void;
}

interface ValidationState {
  valid: boolean;
  error: string | null;
}

const REQUIRED_FIELDS: Array<{
  key: "project_id" | "client_email" | "private_key";
  label: string;
}> = [
  { key: "project_id", label: "project_id" },
  { key: "client_email", label: "client_email" },
  { key: "private_key", label: "private_key" },
];

function validateServiceAccountJson(raw: string): ValidationState {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { valid: false, error: null };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { valid: false, error: "Invalid JSON — check for syntax errors." };
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { valid: false, error: "Expected a JSON object." };
  }

  const obj = parsed as Record<string, unknown>;

  if (obj.type !== "service_account") {
    return {
      valid: false,
      error: 'Field "type" must equal "service_account".',
    };
  }

  for (const { key, label } of REQUIRED_FIELDS) {
    const value = obj[key];
    if (typeof value !== "string" || value.trim() === "") {
      return { valid: false, error: `Missing or empty field: "${label}".` };
    }
  }

  return { valid: true, error: null };
}

export function GcpServiceAccountForm({ onSuccess }: GcpServiceAccountFormProps) {
  const { toast } = useToast();
  const [saJsonText, setSaJsonText] = useState("");
  const [fileName, setFileName] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const validation = useMemo(
    () => validateServiceAccountJson(saJsonText),
    [saJsonText],
  );

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // Clear the input so selecting the same file twice still fires change.
    event.target.value = "";
    if (!file) return;

    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        setSaJsonText("");
        setFileName("");
        setSubmitError(null);
        toast({
          title: "Failed to read file",
          description: "Could not read the selected file as text.",
          variant: "destructive",
        });
        return;
      }
      setSaJsonText(result);
      setFileName(file.name);
      setSubmitError(null);
    };
    reader.onerror = () => {
      setSaJsonText("");
      setFileName("");
      setSubmitError(null);
      toast({
        title: "Failed to read file",
        description: "An error occurred while reading the file.",
        variant: "destructive",
      });
    };
    reader.readAsText(file);
  };

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!validation.valid || loading) return;

    setLoading(true);
    setSubmitError(null);

    try {
      const response = await fetch("/api/gcp/service-account/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ service_account_json: saJsonText }),
      });

      const text = await response.text();
      let payload: unknown = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
      }

      if (!response.ok) {
        const errorMessage =
          payload &&
          typeof payload === "object" &&
          "error" in payload &&
          typeof (payload as { error: unknown }).error === "string"
            ? (payload as { error: string }).error
            : "Failed to connect — try again.";
        setSubmitError(errorMessage);
        toast({
          title: "Failed to connect GCP",
          description: errorMessage,
          variant: "destructive",
        });
        return;
      }

      const successPayload =
        payload && typeof payload === "object"
          ? (payload as {
              email?: string;
              default_project_id?: string;
              accessible_projects?: unknown;
            })
          : {};
      const accessibleCount = Array.isArray(successPayload.accessible_projects)
        ? successPayload.accessible_projects.length
        : null;
      const description = successPayload.email
        ? accessibleCount !== null
          ? `Connected as ${successPayload.email} — ${accessibleCount} project${accessibleCount === 1 ? "" : "s"} accessible.`
          : `Connected as ${successPayload.email}.`
        : "Service account connected successfully.";

      toast({
        title: "GCP connected",
        description,
      });

      setSaJsonText("");
      setFileName("");

      ProjectCache.invalidate("gcp");
      // Fire-and-forget: don't block dialog close on a potentially slow
      // connector-status refetch. Listeners below will re-pull on their own.
      void fetchConnectedAccounts(true).catch(() => {});
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }

      onSuccess();
    } catch (error: unknown) {
      const message =
        error instanceof Error && error.message
          ? error.message
          : "Failed to connect — try again.";
      setSubmitError(message);
      toast({
        title: "Failed to connect GCP",
        description: message,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const isDirty = saJsonText.trim().length > 0;
  const showInlineValidationError = isDirty && !validation.valid && validation.error;

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div className="grid gap-1.5">
        <Label className="text-sm font-medium">Service Account Key (JSON)</Label>
        <div className="flex items-center gap-3">
          <Label
            htmlFor="sa-upload"
            className="flex items-center gap-2 px-4 py-2 rounded-md border cursor-pointer hover:bg-muted/50 transition-colors text-sm"
          >
            <Upload className="h-4 w-4" />
            {fileName || "Choose JSON key file"}
          </Label>
          <input
            id="sa-upload"
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={handleFileChange}
            disabled={loading}
          />
          {validation.valid && (
            <Badge variant="secondary" className="text-xs">
              <Check className="h-3 w-3 mr-1" /> Valid
            </Badge>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          The file is read in your browser and never uploaded — only the JSON
          text is sent to Aurora.
        </p>
      </div>

      <div className="grid gap-1.5">
        <Label htmlFor="sa-json-text" className="text-sm font-medium">
          Or paste the JSON directly
        </Label>
        <Textarea
          id="sa-json-text"
          value={saJsonText}
          onChange={(event) => {
            setSaJsonText(event.target.value);
            if (fileName) setFileName("");
            if (submitError) setSubmitError(null);
          }}
          placeholder={'{ "type": "service_account", "project_id": "...", ... }'}
          className="min-h-[160px] font-mono text-xs"
          disabled={loading}
          spellCheck={false}
          autoComplete="off"
        />
        {showInlineValidationError && (
          <p className="text-xs text-destructive flex items-center gap-1.5">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            {validation.error}
          </p>
        )}
      </div>

      <Alert>
        <AlertTitle className="text-sm">Auto-discover all your projects</AlertTitle>
        <AlertDescription className="text-xs text-muted-foreground">
          To auto-discover all your projects with one key, grant the service
          account <code className="font-mono">roles/browser</code> (or{" "}
          <code className="font-mono">roles/viewer</code>) at the organization
          or folder level in GCP IAM. Otherwise only the project this key was
          created in will be connected.
        </AlertDescription>
      </Alert>

      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
        <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">Your key is encrypted at rest</p>
          <p className="text-muted-foreground">
            Your key is stored encrypted in Aurora&apos;s secrets vault. You can
            disconnect anytime. The file you upload is read in your browser and
            never transmitted — only the JSON text is sent to Aurora.
          </p>
        </div>
      </div>

      {submitError && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Failed to connect</AlertTitle>
          <AlertDescription className="text-sm">{submitError}</AlertDescription>
        </Alert>
      )}

      <Button
        type="submit"
        disabled={loading || !validation.valid}
        className="w-full h-10"
      >
        {loading ? (
          <>
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            Connecting...
          </>
        ) : (
          "Connect with Service Account"
        )}
      </Button>
    </form>
  );
}

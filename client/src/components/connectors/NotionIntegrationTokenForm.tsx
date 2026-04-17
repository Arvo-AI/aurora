"use client";

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertCircle,
  Eye,
  EyeOff,
  Loader2,
  ShieldCheck,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";

interface NotionIntegrationTokenFormProps {
  onSuccess: () => void;
}

interface ValidationState {
  valid: boolean;
  error: string | null;
}

const VALIDATION_REVEAL_CHARS = 10;

function validateNotionToken(raw: string): ValidationState {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { valid: false, error: null };
  }

  if (!trimmed.startsWith("secret_") && !trimmed.startsWith("ntn_")) {
    return {
      valid: false,
      error: 'Token must start with "secret_" or "ntn_".',
    };
  }

  if (trimmed.length < 40) {
    return {
      valid: false,
      error: "Token looks too short — double-check you copied the full secret.",
    };
  }

  if (trimmed.length > 200) {
    return {
      valid: false,
      error: "Token is longer than expected — double-check you copied only the secret.",
    };
  }

  return { valid: true, error: null };
}

function maskToken(value: string): string {
  return value.replace(/\S/g, "\u2022");
}

export function NotionIntegrationTokenForm({
  onSuccess,
}: NotionIntegrationTokenFormProps) {
  const { toast } = useToast();
  const [token, setToken] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  const validation = useMemo(() => validateNotionToken(token), [token]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!validation.valid || loading) return;

    setLoading(true);
    setSubmitError(null);

    try {
      const response = await fetch("/api/notion/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ token_type: "iit", token: token.trim() }),
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
          title: "Failed to connect Notion",
          description: errorMessage,
          variant: "destructive",
        });
        return;
      }

      const successPayload =
        payload && typeof payload === "object"
          ? (payload as { workspaceName?: string; workspace_name?: string })
          : {};
      const workspaceName =
        successPayload.workspaceName ?? successPayload.workspace_name ?? null;
      const description = workspaceName
        ? `Notion connected — ${workspaceName}`
        : "Notion connected successfully.";

      toast({
        title: "Notion connected",
        description,
      });

      setToken("");
      setRevealed(false);

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
        title: "Failed to connect Notion",
        description: message,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };


  const isDirty = token.trim().length > 0;
  const meetsRevealThreshold = token.trim().length >= VALIDATION_REVEAL_CHARS;
  const showInlineValidationError =
    isDirty &&
    !validation.valid &&
    !!validation.error &&
    (meetsRevealThreshold || touched);
  const displayValue = revealed ? token : maskToken(token);

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <Alert>
        <AlertTitle className="text-sm">Create a Notion integration</AlertTitle>
        <AlertDescription className="text-xs text-muted-foreground space-y-1.5">
          <p>
            1. Open{" "}
            <a
              href="https://www.notion.so/my-integrations"
              target="_blank"
              rel="noreferrer noopener"
              className="underline underline-offset-2"
            >
              notion.so/my-integrations
            </a>{" "}
            &rarr; New integration &rarr; name it &ldquo;Aurora&rdquo; &rarr; copy the
            Internal Integration Secret.
          </p>
          <p>
            2. Open any page or database you want Aurora to see &rarr; &hellip; menu
            &rarr; Connect to &rarr; Aurora.
          </p>
          <p>3. Paste the secret below.</p>
        </AlertDescription>
      </Alert>

      <div className="grid gap-1.5">
        <div className="flex items-center justify-between">
          <Label htmlFor="notion-iit" className="text-sm font-medium">
            Internal Integration Secret
          </Label>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-xs"
            onClick={() => setRevealed((prev) => !prev)}
            disabled={!isDirty || loading}
            aria-label={revealed ? "Hide token" : "Show token"}
            aria-pressed={revealed}
          >
            {revealed ? (
              <>
                <EyeOff className="h-3.5 w-3.5 mr-1.5" aria-hidden="true" />
                Hide
              </>
            ) : (
              <>
                <Eye className="h-3.5 w-3.5 mr-1.5" aria-hidden="true" />
                Show
              </>
            )}
          </Button>
        </div>
        <Textarea
          id="notion-iit"
          value={displayValue}
          onChange={(event) => {
            const next = event.target.value;
            if (revealed) {
              setToken(next);
            } else if (next.length < token.length) {
              // Backspace/clear while masked — operate on underlying token.
              setToken(token.slice(0, next.length));
            } else if (token.length === 0) {
              setToken(next);
            } else {
              // Additive edit while masked: flip to revealed so the user
              // sees what they're typing instead of keystrokes disappearing.
              // The new character was appended to the masked display, so the
              // token grows by (next.length - token.length) chars from the
              // end of the visible value.
              setRevealed(true);
              const appended = next.slice(token.length);
              setToken(token + appended);
            }
            if (submitError) setSubmitError(null);
          }}
          onBlur={() => setTouched(true)}
          placeholder="ntn_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
          rows={3}
          className="font-mono text-xs"
          disabled={loading}
          spellCheck={false}
          autoComplete="off"
          aria-invalid={showInlineValidationError || undefined}
          aria-describedby={
            showInlineValidationError ? "notion-iit-error" : undefined
          }
        />
        {showInlineValidationError && (
          <p
            id="notion-iit-error"
            className="text-xs text-destructive flex items-center gap-1.5"
          >
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            {validation.error}
          </p>
        )}
      </div>

      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
        <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">Your token is encrypted at rest</p>
          <p className="text-muted-foreground">
            The secret is stored encrypted in Aurora&apos;s secrets vault. You
            can disconnect anytime, and Aurora will only see pages and
            databases you explicitly share with the integration.
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
          "Connect with Integration Token"
        )}
      </Button>
    </form>
  );
}

export default NotionIntegrationTokenForm;

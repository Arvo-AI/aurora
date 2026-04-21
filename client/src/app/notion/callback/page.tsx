"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Loader2, CheckCircle, XCircle } from "lucide-react";

type Status = "loading" | "success" | "error" | "orphan";

function NotionCallbackInner() {
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<Status>("loading");
  const [message, setMessage] = useState("Finishing Notion sign-in...");

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const error = searchParams.get("error");
    const errorDescription = searchParams.get("error_description");
    const code = searchParams.get("code");
    const state = searchParams.get("state");
    const targetOrigin = window.location.origin;

    if (error) {
      setStatus("error");
      setMessage(errorDescription || error);
      if (window.opener) {
        try {
          window.opener.postMessage(
            { type: "notion-auth-error", error, errorDescription },
            targetOrigin,
          );
        } catch {
          // best-effort; ignore cross-origin issues
        }
        window.setTimeout(() => window.close(), 500);
      }
      return;
    }

    if (!code || !state) {
      setStatus("error");
      setMessage("Missing authorization code or state.");
      return;
    }

    if (!window.opener) {
      // Direct visit (no opener) — do NOT auto-redirect; user may have
      // stumbled onto the URL. Just inform them they can close this tab.
      setStatus("orphan");
      setMessage("You can close this tab.");
      return;
    }

    try {
      window.opener.postMessage(
        { type: "notion-auth-success", code, state },
        targetOrigin,
      );
      setStatus("success");
      setMessage("Notion connected — closing this tab...");
      window.setTimeout(() => window.close(), 400);
    } catch (err) {
      console.error("Notion callback postMessage failed", err);
      setStatus("error");
      setMessage("Could not hand off to Aurora. Please close this tab and try again.");
    }
  }, [searchParams]);

  return (
    <div className="flex items-center justify-center min-h-screen bg-background">
      <div className="text-center space-y-4 p-8 max-w-md">
        {status === "loading" && (
          <>
            <Loader2 className="h-12 w-12 animate-spin mx-auto text-muted-foreground" />
            <p className="text-lg font-medium">{message}</p>
          </>
        )}
        {status === "success" && (
          <>
            <CheckCircle className="h-12 w-12 mx-auto text-green-600 dark:text-green-500" />
            <p className="text-lg font-medium">{message}</p>
          </>
        )}
        {status === "orphan" && (
          <>
            <CheckCircle className="h-12 w-12 mx-auto text-green-600 dark:text-green-500" />
            <p className="text-lg font-medium">Notion sign-in complete</p>
            <p className="text-sm text-muted-foreground">{message}</p>
          </>
        )}
        {status === "error" && (
          <>
            <XCircle className="h-12 w-12 mx-auto text-destructive" />
            <p className="text-lg font-medium text-destructive">{message}</p>
            <p className="text-sm text-muted-foreground">
              You can close this tab and try again from Aurora.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

export default function NotionCallbackPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center min-h-screen bg-background">
          <Loader2 className="h-12 w-12 animate-spin text-muted-foreground" />
        </div>
      }
    >
      <NotionCallbackInner />
    </Suspense>
  );
}

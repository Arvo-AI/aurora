"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Loader2, CheckCircle, XCircle } from "lucide-react";

type Status = "loading" | "success" | "error" | "orphan";

/**
 * OAuth landing page for customer-registered MCP servers.
 *
 * Hands the code and state to the opener and closes. The code is never
 * exchanged here: that needs the PKCE verifier, which stays server-side.
 */
function McpCallbackInner() {
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<Status>("loading");
  const [message, setMessage] = useState("Finishing MCP server authorization...");

  useEffect(() => {
    if (typeof window === "undefined") return;

    const error = searchParams.get("error");
    const errorDescription = searchParams.get("error_description");
    const code = searchParams.get("code");
    const state = searchParams.get("state");
    // Same-origin only: the code must not be broadcast to any other site.
    const targetOrigin = window.location.origin;

    if (error) {
      setStatus("error");
      setMessage(errorDescription || error);
      if (window.opener) {
        try {
          window.opener.postMessage(
            { type: "mcp-auth-error", error, errorDescription },
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
      // Direct visit: do not act on the code, the user may have stumbled here.
      setStatus("orphan");
      setMessage("You can close this tab.");
      return;
    }

    try {
      window.opener.postMessage({ type: "mcp-auth-success", code, state }, targetOrigin);
      setStatus("success");
      setMessage("Authorized - closing this tab...");
      window.setTimeout(() => window.close(), 400);
    } catch (err) {
      console.error("MCP callback postMessage failed", err);
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
            <p className="text-lg font-medium">Authorization complete</p>
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

export default function McpCallbackPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center min-h-screen bg-background">
          <Loader2 className="h-12 w-12 animate-spin text-muted-foreground" />
        </div>
      }
    >
      <McpCallbackInner />
    </Suspense>
  );
}

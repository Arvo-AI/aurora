"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, CheckCircle, XCircle } from "lucide-react";
import { confluenceService } from "@/lib/services/confluence";

type Status = "loading" | "success" | "error";

const OAUTH_IN_PROGRESS_TTL_MS = 30_000;
const POLL_INTERVAL_MS = 1000;

export default function ConfluenceCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<Status>("loading");
  const [message, setMessage] = useState("Processing Confluence OAuth callback...");

  useEffect(() => {
    let isActive = true;
    let pollTimeoutId: number | null = null;
    const error = searchParams.get("error");
    const errorDescription = searchParams.get("error_description");
    const code = searchParams.get("code");
    const state = searchParams.get("state");
    const sessionKey = code ? `confluenceOauthCode:${code}` : null;

    const clearSessionMarker = () => {
      if (typeof window === "undefined" || !sessionKey) {
        return;
      }
      window.sessionStorage.removeItem(sessionKey);
    };

    const parseSessionMarker = (value: string | null) => {
      if (!value) {
        return null;
      }
      if (value === "done") {
        return { status: "done" as const, startedAt: null };
      }
      if (value === "in-progress") {
        return { status: "in-progress" as const, startedAt: null };
      }
      if (value.startsWith("in-progress:")) {
        const startedAt = Number(value.split(":", 2)[1]);
        if (!Number.isNaN(startedAt)) {
          return { status: "in-progress" as const, startedAt };
        }
      }
      return null;
    };

    if (error) {
      if (isActive) {
        setStatus("error");
        setMessage(errorDescription || error);
      }
      clearSessionMarker();
      return () => {
        isActive = false;
      };
    }

    if (!code) {
      if (isActive) {
        setStatus("error");
        setMessage("Missing authorization code.");
      }
      clearSessionMarker();
      return () => {
        isActive = false;
      };
    }

    if (!state) {
      if (isActive) {
        setStatus("error");
        setMessage("Missing OAuth state.");
      }
      clearSessionMarker();
      return () => {
        isActive = false;
      };
    }

    if (typeof window !== "undefined") {
      const cached = parseSessionMarker(window.sessionStorage.getItem(sessionKey));
      const startedAt = cached?.startedAt ?? 0;
      const isStale = cached?.status === "in-progress" && (!startedAt || Date.now() - startedAt > OAUTH_IN_PROGRESS_TTL_MS);

      if (cached?.status === "done") {
        router.replace("/confluence/connect");
        return () => {
          isActive = false;
        };
      }
      if (cached?.status === "in-progress" && !isStale) {
        setMessage("Waiting for Confluence OAuth to finish...");
        const pollForCompletion = () => {
          if (!isActive || typeof window === "undefined" || !sessionKey) {
            return;
          }
          const current = parseSessionMarker(window.sessionStorage.getItem(sessionKey));
          if (current?.status === "done") {
            router.replace("/confluence/connect");
            return;
          }
          if (Date.now() - startedAt > OAUTH_IN_PROGRESS_TTL_MS) {
            clearSessionMarker();
            if (isActive) {
              setStatus("error");
              setMessage("Previous OAuth attempt timed out. Please try again.");
            }
            return;
          }
          pollTimeoutId = window.setTimeout(pollForCompletion, POLL_INTERVAL_MS);
        };
        pollForCompletion();
        return () => {
          isActive = false;
          if (pollTimeoutId) {
            clearTimeout(pollTimeoutId);
          }
        };
      }

      if (isStale) {
        clearSessionMarker();
      }
      window.sessionStorage.setItem(sessionKey, `in-progress:${Date.now()}`);
    }

    const exchangeCode = async () => {
      try {
        const result = await confluenceService.connect({ authType: "oauth", code, state });
        if (!result?.connected) {
          throw new Error("Confluence OAuth failed.");
        }

        if (typeof window !== "undefined") {
          localStorage.setItem("isConfluenceConnected", "true");
          window.sessionStorage.setItem(sessionKey, "done");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }

        if (!isActive) {
          return;
        }
        setStatus("success");
        setMessage("Confluence connected successfully!");

        setTimeout(() => {
          router.replace("/confluence/connect");
        }, 1000);
      } catch (err) {
        try {
          const statusResult = await confluenceService.getStatus();
          if (statusResult?.connected) {
            if (typeof window !== "undefined") {
              localStorage.setItem("isConfluenceConnected", "true");
              window.sessionStorage.setItem(sessionKey, "done");
              window.dispatchEvent(new CustomEvent("providerStateChanged"));
            }
            if (!isActive) {
              return;
            }
            setStatus("success");
            setMessage("Confluence connected successfully!");
            setTimeout(() => {
              router.replace("/confluence/connect");
            }, 500);
            return;
          }
        } catch (statusError) {
          console.error("Failed to check Confluence status after OAuth error", statusError);
        }

        clearSessionMarker();
        if (!isActive) {
          return;
        }
        const errorMessage = err instanceof Error ? err.message : "OAuth exchange failed.";
        setStatus("error");
        setMessage(errorMessage);
      }
    };

    exchangeCode();

    return () => {
      isActive = false;
    };
  }, [searchParams, router]);

  return (
    <div className="flex items-center justify-center min-h-screen bg-background">
      <div className="text-center space-y-4 p-8">
        {status === "loading" && (
          <>
            <Loader2 className="h-12 w-12 animate-spin mx-auto text-primary" />
            <p className="text-lg font-medium">{message}</p>
          </>
        )}
        {status === "success" && (
          <>
            <CheckCircle className="h-12 w-12 mx-auto text-green-600" />
            <p className="text-lg font-medium text-green-600">{message}</p>
            <p className="text-sm text-muted-foreground">Redirecting...</p>
          </>
        )}
        {status === "error" && (
          <>
            <XCircle className="h-12 w-12 mx-auto text-red-600" />
            <p className="text-lg font-medium text-red-600">{message}</p>
            <button
              className="px-4 py-2 rounded-md bg-primary text-primary-foreground"
              onClick={() => router.replace("/confluence/connect")}
            >
              Back to Confluence setup
            </button>
          </>
        )}
      </div>
    </div>
  );
}

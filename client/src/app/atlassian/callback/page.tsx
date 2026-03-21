"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, CheckCircle, XCircle } from "lucide-react";
import { atlassianService } from "@/lib/services/atlassian";

type Status = "loading" | "success" | "error";

export default function AtlassianCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<Status>("loading");
  const [message, setMessage] = useState("Processing Atlassian OAuth callback...");
  const exchangedRef = useRef(false);

  useEffect(() => {
    if (exchangedRef.current) return;

    const error = searchParams.get("error");
    const errorDescription = searchParams.get("error_description");
    const code = searchParams.get("code");
    const state = searchParams.get("state");

    if (error) {
      setStatus("error");
      setMessage(errorDescription || error);
      return;
    }

    if (!code || !state) {
      setStatus("error");
      setMessage("Missing authorization code or state.");
      return;
    }

    exchangedRef.current = true;

    const exchangeCode = async () => {
      try {
        const result = await atlassianService.connect({
          products: ["confluence", "jira"],
          authType: "oauth",
          code,
          state,
        });

        if (!result?.connected && !result?.success) {
          throw new Error("Atlassian OAuth failed.");
        }

        const results = result?.results || {};
        const jiraConnected = results.jira?.connected;
        const confluenceConnected = results.confluence?.connected;
        const jiraError = results.jira?.error;
        const confluenceError = results.confluence?.error;

        if (typeof window !== "undefined") {
          if (confluenceConnected) localStorage.setItem("isConfluenceConnected", "true");
          if (jiraConnected) localStorage.setItem("isJiraConnected", "true");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }

        if (!jiraConnected && !confluenceConnected) {
          const detail = jiraError || confluenceError || "Token may lack required scopes.";
          throw new Error(`No products connected. ${detail}`);
        }

        setStatus("success");
        const parts: string[] = [];
        if (jiraConnected) parts.push("Jira");
        if (confluenceConnected) parts.push("Confluence");
        setMessage(`${parts.join(" & ")} connected!`);

        const returnTo = jiraConnected && !confluenceConnected ? "/jira/connect" : "/confluence/connect";
        setTimeout(() => router.replace(returnTo), 1000);
      } catch (err) {
        try {
          const statusResult = await atlassianService.getStatus();
          const anyConnected = statusResult?.confluence?.connected || statusResult?.jira?.connected;
          if (anyConnected) {
            if (typeof window !== "undefined") {
              if (statusResult?.confluence?.connected) localStorage.setItem("isConfluenceConnected", "true");
              if (statusResult?.jira?.connected) localStorage.setItem("isJiraConnected", "true");
              window.dispatchEvent(new CustomEvent("providerStateChanged"));
            }
            setStatus("success");
            setMessage("Connected successfully!");
            const returnTo = statusResult?.jira?.connected && !statusResult?.confluence?.connected ? "/jira/connect" : "/confluence/connect";
            setTimeout(() => router.replace(returnTo), 500);
            return;
          }
        } catch (statusErr) {
          console.warn('[AtlassianCallback] Status check failed:', statusErr);
        }

        setStatus("error");
        setMessage(err instanceof Error ? err.message : "OAuth exchange failed.");
      }
    };

    exchangeCode();
  }, [searchParams, router]);

  return (
    <div className="flex items-center justify-center min-h-screen bg-background">
      <div className="text-center space-y-4 p-8">
        {status === "loading" && (
          <>
            <Loader2 className="h-12 w-12 animate-spin mx-auto text-[#2684FF]" />
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
              onClick={() => router.replace("/jira/connect")}
            >
              Back to setup
            </button>
          </>
        )}
      </div>
    </div>
  );
}

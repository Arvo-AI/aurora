"use client";

import { useEffect, useRef } from "react";
import { getEnv } from "@/lib/env";

const SETUP_KEYS = ["gcpSetupTaskId", "gcpSetupInProgress", "gcpSetupInProgress_timestamp"] as const;

/**
 * Polls the GCP post-auth Celery task after OAuth redirect.
 * On failure, persists error to localStorage("gcpSetupError") for the connector card UI.
 */
export function useGcpSetupPolling(userId: string | null) {
  const intervalRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    // Capture task_id from OAuth redirect URL
    const params = new URLSearchParams(window.location.search);
    const taskId = params.get("login") === "gcp_setup_pending" ? params.get("task_id") : null;
    if (taskId) {
      localStorage.setItem("gcpSetupTaskId", taskId);
      localStorage.setItem("gcpSetupInProgress", "true");
      localStorage.setItem("gcpSetupInProgress_timestamp", Date.now().toString());
      localStorage.removeItem("gcpSetupError");
      window.history.replaceState({}, "", window.location.pathname);
    }

    // Start polling if there's a pending task and we have auth
    const pendingTaskId = localStorage.getItem("gcpSetupTaskId");
    if (!pendingTaskId || !userId || localStorage.getItem("gcpSetupInProgress") !== "true") return;

    const startedAt = parseInt(localStorage.getItem("gcpSetupInProgress_timestamp") || "0", 10);
    if (startedAt && Date.now() - startedAt > 30 * 60 * 1000) {
      SETUP_KEYS.forEach((k) => localStorage.removeItem(k));
      return;
    }

    const backendUrl = getEnv("NEXT_PUBLIC_BACKEND_URL");
    let attempts = 0;

    intervalRef.current = setInterval(async () => {
      attempts++;
      try {
        const res = await fetch(`${backendUrl}/gcp/setup/status/${pendingTaskId}`, {
          credentials: "include",
          headers: { "X-User-ID": userId },
        });
        const data = await res.json();
        if (!data.complete && attempts < 360) return;

        clearInterval(intervalRef.current!);
        SETUP_KEYS.forEach((k) => localStorage.removeItem(k));

        const result = data.result || {};
        if (result.status === "FAILED") {
          localStorage.setItem("gcpSetupError", result.error || "GCP connection failed during setup.");
          fetch("/api/connected-accounts/gcp", { method: "DELETE", credentials: "include" }).catch(() => {});
        } else if (result.status === "needs_selection") {
          localStorage.setItem("gcpNeedsProjectSelection", "true");
          localStorage.setItem("gcpEligibleProjects", JSON.stringify(result.eligible_projects));
          window.dispatchEvent(new Event("gcpProjectSelectionNeeded"));
        }
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      } catch {
        if (attempts >= 360) clearInterval(intervalRef.current!);
      }
    }, 5000);

    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [userId]);
}

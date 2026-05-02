"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { ProjectSelectionModal } from "@/components/ProjectSelectionModal";
import { useToast } from "@/hooks/use-toast";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";

/**
 * Global component that monitors for GCP project selection needs and polls
 * the Celery post-auth setup task after OAuth redirect.
 *
 * Mounted at the app root level (ClientShell) so it works regardless of the
 * current page.
 */
export default function GlobalProjectSelectionMonitor() {
  const [projectSelectionOpen, setProjectSelectionOpen] = useState(false);
  const [eligibleProjects, setEligibleProjects] = useState<any[]>([]);
  const { toast } = useToast();
  const abortRef = useRef<AbortController | null>(null);

  const pollTask = useCallback(
    async (taskId: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      const MAX_ATTEMPTS = 360;
      let attempts = 0;

      while (!controller.signal.aborted && attempts < MAX_ATTEMPTS) {
        attempts++;
        try {
          const res = await fetch(
            `/api/proxy/gcp/setup/status/${encodeURIComponent(taskId)}`,
            { signal: controller.signal },
          );
          const data = await res.json();

          if (!data.complete) {
            await new Promise((r) => setTimeout(r, 5000));
            continue;
          }

          if (data.state === "SUCCESS" && data.result?.status === "needs_selection") {
            localStorage.setItem("gcpNeedsProjectSelection", "true");
            localStorage.setItem(
              "gcpEligibleProjects",
              JSON.stringify(data.result.eligible_projects),
            );
            cleanupSetupState();
            window.dispatchEvent(new Event("gcpProjectSelectionNeeded"));
            return;
          }

          if (data.state === "SUCCESS") {
            cleanupSetupState();
            localStorage.setItem("isGCPFetched", "false");
            await fetchConnectedAccounts(true).catch(() => {});
            window.dispatchEvent(new CustomEvent("providerStateChanged"));
            toast({ title: "GCP connected", description: "Setup completed successfully." });
            return;
          }

          // Task failed
          cleanupSetupState();
          const msg =
            data.result?.redirect_params?.includes("gcp_no_projects")
              ? "No GCP projects found."
              : data.result?.redirect_params?.includes("gcp_failed_billing")
                ? "No GCP project has billing enabled."
                : "GCP setup failed.";
          toast({ title: "GCP Setup Error", description: msg, variant: "destructive" });
          return;
        } catch (err: any) {
          if (err.name === "AbortError") return;
          console.error("[GcpSetupPoll] poll error:", err);
          await new Promise((r) => setTimeout(r, 5000));
        }
      }

      if (attempts >= MAX_ATTEMPTS) {
        cleanupSetupState();
        toast({
          title: "GCP Setup Timeout",
          description: "Setup took too long. Please try reconnecting.",
          variant: "destructive",
        });
      }
    },
    [toast],
  );

  // Pick up task_id from URL after OAuth redirect and start polling
  useEffect(() => {
    if (typeof window === "undefined") return;

    const params = new URLSearchParams(window.location.search);
    const login = params.get("login");
    const taskId = params.get("task_id");

    if (login === "gcp_setup_pending" && taskId) {
      // Clean the URL so a refresh doesn't re-trigger
      const url = new URL(window.location.href);
      url.searchParams.delete("login");
      url.searchParams.delete("task_id");
      window.history.replaceState({}, "", url.toString());

      localStorage.setItem("gcpSetupInProgress", "true");
      localStorage.setItem("gcpSetupTaskId", taskId);
      pollTask(taskId);
      return;
    }

    // Resume polling if a task was in flight (e.g. page refresh during setup)
    const inProgress = localStorage.getItem("gcpSetupInProgress") === "true";
    const storedTaskId = localStorage.getItem("gcpSetupTaskId");
    if (inProgress && storedTaskId) {
      pollTask(storedTaskId);
    }

    return () => abortRef.current?.abort();
  }, [pollTask]);

  // Resume polling after user selects projects in the modal
  useEffect(() => {
    const handleRetry = () => {
      const taskId = localStorage.getItem("gcpSetupTaskId");
      if (taskId) pollTask(taskId);
    };

    window.addEventListener("gcpSetupRetryStarted", handleRetry);
    return () => window.removeEventListener("gcpSetupRetryStarted", handleRetry);
  }, [pollTask]);

  // Monitor localStorage for project selection needs
  useEffect(() => {
    const checkForProjectSelection = () => {
      const needsSelection = localStorage.getItem("gcpNeedsProjectSelection") === "true";
      if (needsSelection) {
        const projectsJson = localStorage.getItem("gcpEligibleProjects");
        if (projectsJson) {
          try {
            const projects = JSON.parse(projectsJson);
            setEligibleProjects(projects);
            setProjectSelectionOpen(true);
          } catch (error) {
            console.error("Failed to parse eligible projects:", error);
          }
        }
      }
    };

    checkForProjectSelection();

    const handleStorageChange = (e: StorageEvent) => {
      if (e.key === "gcpNeedsProjectSelection" && e.newValue === "true") {
        checkForProjectSelection();
      }
    };

    const handleCustomEvent = () => checkForProjectSelection();

    window.addEventListener("storage", handleStorageChange);
    window.addEventListener("gcpProjectSelectionNeeded", handleCustomEvent);

    return () => {
      window.removeEventListener("storage", handleStorageChange);
      window.removeEventListener("gcpProjectSelectionNeeded", handleCustomEvent);
    };
  }, []);

  const handleProjectSelection = async (selectedProjectIds: string[]) => {
    try {
      const retryResponse = await fetch(`/api/proxy/gcp/post-auth-retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_project_ids: selectedProjectIds }),
      });

      if (!retryResponse.ok) {
        throw new Error(`Retry failed: ${retryResponse.statusText}`);
      }

      const result = await retryResponse.json();

      if (result.task_id) {
        localStorage.setItem("gcpSetupInProgress", "true");
        localStorage.setItem("gcpSetupTaskId", result.task_id);
        localStorage.removeItem("gcpPollingActive");
      }

      localStorage.removeItem("gcpNeedsProjectSelection");
      localStorage.removeItem("gcpEligibleProjects");
      setProjectSelectionOpen(false);

      window.dispatchEvent(new Event("gcpSetupRetryStarted"));
    } catch (error) {
      console.error("Error re-triggering setup with selected projects:", error);
      toast({
        title: "Error",
        description: "Failed to start project indexing. Please try again.",
        variant: "destructive",
      });
    }
  };

  const handleCancel = () => {
    localStorage.removeItem("gcpNeedsProjectSelection");
    localStorage.removeItem("gcpEligibleProjects");
    setProjectSelectionOpen(false);
  };

  return (
    <ProjectSelectionModal
      open={projectSelectionOpen}
      projects={eligibleProjects}
      onSelect={handleProjectSelection}
      onCancel={handleCancel}
    />
  );
}

function cleanupSetupState() {
  localStorage.removeItem("gcpSetupInProgress");
  localStorage.removeItem("gcpSetupInProgress_timestamp");
  localStorage.removeItem("gcpSetupTaskId");
  localStorage.removeItem("gcpPollingActive");
}


"use client";

import { useState, useEffect } from "react";
import { ProjectSelectionModal } from "@/components/ProjectSelectionModal";
import { useToast } from "@/hooks/use-toast";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL;

/**
 * Global component that monitors for GCP project selection needs
 * This is mounted at the app root level so the modal appears regardless of the current page
 */
export default function GlobalProjectSelectionMonitor() {
  const [projectSelectionOpen, setProjectSelectionOpen] = useState(false);
  const [eligibleProjects, setEligibleProjects] = useState<any[]>([]);
  const { toast } = useToast();

  // Monitor localStorage for project selection needs on mount and storage changes
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

    // Check immediately on mount
    checkForProjectSelection();

    // Listen for storage events (when another tab updates localStorage)
    const handleStorageChange = (e: StorageEvent) => {
      if (e.key === "gcpNeedsProjectSelection" && e.newValue === "true") {
        checkForProjectSelection();
      }
    };

    // Listen for custom events (when same tab updates localStorage)
    const handleCustomEvent = () => {
      checkForProjectSelection();
    };

    window.addEventListener("storage", handleStorageChange);
    window.addEventListener("gcpProjectSelectionNeeded", handleCustomEvent);

    return () => {
      window.removeEventListener("storage", handleStorageChange);
      window.removeEventListener("gcpProjectSelectionNeeded", handleCustomEvent);
    };
  }, []);

  const handleProjectSelection = async (selectedProjectIds: string[]) => {
    try {
      const response = await fetch("/api/getUserId");
      
      if (!response.ok) {
        console.error("Failed to fetch user ID:", response.statusText);
        toast({
          title: "Error",
          description: "Failed to retrieve user information. Please try again.",
          variant: "destructive",
        });
        return;
      }
      
      const { userId } = await response.json();

      if (!userId) {
        console.error("No user ID found");
        toast({
          title: "Error",
          description: "No user ID found. Please log in again.",
          variant: "destructive",
        });
        return;
      }

      // Call the retry endpoint with selected projects
      const retryResponse = await fetch(`${BACKEND_URL}/gcp/post-auth-retry`, {
        method: "POST",
        headers: { 
          "Content-Type": "application/json",
          "X-User-ID": userId, // Send user ID for backend authentication
        },
        credentials: "include", // Include session cookie for authentication
        body: JSON.stringify({
          selected_project_ids: selectedProjectIds,
        }),
      });

      if (!retryResponse.ok) {
        throw new Error(`Retry failed: ${retryResponse.statusText}`);
      }

      const result = await retryResponse.json();

      // Set up polling state for the new task
      if (result.task_id) {
        localStorage.setItem("gcpSetupInProgress", "true");
        localStorage.setItem("gcpSetupTaskId", result.task_id);
        localStorage.removeItem("gcpPollingActive"); // Force polling to start
      }

      // Clear the selection state
      localStorage.removeItem("gcpNeedsProjectSelection");
      localStorage.removeItem("gcpEligibleProjects");

      // Close modal
      setProjectSelectionOpen(false);

      // Notify that polling should resume
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
    // Clear the selection state
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


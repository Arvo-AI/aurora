"use client";

import { useEffect, useState } from "react";
import { useUser } from "@/hooks/useAuthHooks";
import { canWrite } from "@/lib/roles";

/** True when this person may modify memories. Null until a viewer's org switch is known. */
export function useCanEditMemory(): boolean | null {
  const { user } = useUser();
  const role = user?.role;
  const privileged = canWrite(role);
  const [viewerAllowed, setViewerAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    // The session role arrives after the first render.
    if (!role) return;
    // Editors and admins are not gated by the viewer switch.
    if (privileged) {
      setViewerAllowed(true);
      return;
    }
    if (role !== "viewer") {
      setViewerAllowed(false);
      return;
    }
    let cancelled = false;
    fetch("/api/org/viewer-run")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!cancelled) setViewerAllowed(Boolean(data?.memory));
      })
      .catch(() => {
        if (!cancelled) setViewerAllowed(false);
      });
    return () => {
      cancelled = true;
    };
  }, [privileged, role]);

  if (!role) return null;
  return privileged ? true : viewerAllowed;
}

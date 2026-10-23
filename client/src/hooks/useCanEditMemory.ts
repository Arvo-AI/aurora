"use client";

import { useEffect, useState } from "react";
import { useUser } from "@/hooks/useAuthHooks";
import { ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER } from "@/lib/roles";

/** True when this person may modify memories. Null until the org switch is known. */
export function useCanEditMemory(): boolean | null {
  const { user } = useUser();
  const role = user?.role;
  const admin = role === ROLE_ADMIN;
  const [allowed, setAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    // The session role arrives after the first render.
    if (!role) return;
    // Admins are not gated by either memory switch.
    if (admin) {
      setAllowed(true);
      return;
    }
    if (role !== ROLE_VIEWER && role !== ROLE_EDITOR) {
      setAllowed(false);
      return;
    }
    let cancelled = false;
    fetch("/api/org/viewer-run")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        // Editors use their own switch. A missing value stays allowed.
        const on = role === ROLE_EDITOR ? data?.editorMemory !== false : Boolean(data?.memory);
        setAllowed(on);
      })
      .catch(() => {
        if (!cancelled) setAllowed(false);
      });
    return () => {
      cancelled = true;
    };
  }, [admin, role]);

  if (!role) return null;
  if (admin) return true;
  return allowed;
}

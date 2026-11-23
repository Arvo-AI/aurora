"use client";

import { useEffect, useState } from "react";

// One request per page load, shared by both sidebars.
let pending: Promise<boolean> | null = null;

function load(): Promise<boolean> {
  pending ??= fetch("/api/detection/status", { credentials: "include" })
    .then((res) => (res.ok ? res.json() : { enabled: false }))
    .then((data) => Boolean(data?.enabled))
    .catch(() => {
      // Not cached: a failed read is retried on the next mount.
      pending = null;
      return false;
    });
  return pending;
}

/** Whether detection checks are turned on for this install. False until known. */
export function useDetectionEnabled(): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let cancelled = false;
    load()
      .then((value) => {
        if (!cancelled) setEnabled(value);
      })
      .catch(() => {
        // load() resolves false on failure; nothing to do if it ever rejects.
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return enabled;
}

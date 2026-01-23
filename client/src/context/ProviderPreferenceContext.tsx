"use client";

import React, { createContext, useContext, useState, useEffect, ReactNode } from "react";

// Types -------------------------------------------------------------------
export type ProviderPreference = "gcp" | "azure" | "aws" | "grafana";
export type ProviderPreferences = ProviderPreference[];

interface ProviderPreferenceContextValue {
  providerPreferences: ProviderPreferences;
  setProviderPreferences: (prefs: ProviderPreferences) => void;
  addProviderPreference: (pref: ProviderPreference) => void;
  removeProviderPreference: (pref: ProviderPreference) => void;
  hasProviderPreference: (pref: ProviderPreference) => boolean;
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------
const ProviderPreferenceContext = createContext<ProviderPreferenceContextValue>({
  providerPreferences: [],
  setProviderPreferences: () => {},
  addProviderPreference: () => {},
  removeProviderPreference: () => {},
  hasProviderPreference: () => false
});

// ---------------------------------------------------------------------------
// Provider component
// ---------------------------------------------------------------------------
export function ProviderPreferenceProvider({ children }: { children: ReactNode }) {
  const [providerPreferences, setProviderPreferencesState] = useState<ProviderPreferences>([]);

  // Keep preference in localStorage so it survives reloads & page navigation
  useEffect(() => {
    const stored = (typeof window !== "undefined" ? localStorage.getItem("provider_preferences") : null);
    if (stored) {
      try {
        const parsed = JSON.parse(stored) as ProviderPreferences;
        setProviderPreferencesState(parsed);
      } catch (e) {
        // Fallback for old single provider format
        const oldStored = localStorage.getItem("provider_preference");
        if (oldStored && ["gcp", "azure", "aws", "grafana"].includes(oldStored)) {
          setProviderPreferencesState([oldStored as ProviderPreference]);
          // Migrate to new format
          localStorage.setItem("provider_preferences", JSON.stringify([oldStored]));
          localStorage.removeItem("provider_preference");
        }
      }
    }
  }, []);

  const setProviderPreferences = (prefs: ProviderPreferences) => {
    setProviderPreferencesState(prefs);
    if (typeof window !== "undefined") {
      if (prefs.length > 0) {
        localStorage.setItem("provider_preferences", JSON.stringify(prefs));
      } else {
        localStorage.removeItem("provider_preferences");
      }
      // Broadcast change so other tabs/components listening for the built-in
      // `storage` event can react immediately.
      window.dispatchEvent(
        new StorageEvent("storage", { key: "provider_preferences", newValue: JSON.stringify(prefs) })
      );
    }
  };

  const addProviderPreference = (pref: ProviderPreference) => {
    if (!providerPreferences.includes(pref)) {
      setProviderPreferences([...providerPreferences, pref]);
    }
  };

  const removeProviderPreference = (pref: ProviderPreference) => {
    setProviderPreferences(providerPreferences.filter((p: ProviderPreference) => p !== pref));
  };

  const hasProviderPreference = (pref: ProviderPreference) => {
    return providerPreferences.includes(pref);
  };

  return (
    <ProviderPreferenceContext.Provider value={{ 
      providerPreferences, 
      setProviderPreferences, 
      addProviderPreference, 
      removeProviderPreference, 
      hasProviderPreference 
    }}>
      {children}
    </ProviderPreferenceContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// Convenience hook
// ---------------------------------------------------------------------------
export const useProviderPreference = () => useContext(ProviderPreferenceContext); 
"use client";

// TODO: Add a sidebar/nav entry for "Models" at /manage-org/models.
// The existing manage-org/knowledge-base page is reached via SettingsModal
// (hardcoded tabs); when wiring the new admin nav, add a "Models" entry here.

import { ModelRoleSettings } from "@/components/ModelRoleSettings";

export default function ModelsPage() {
  return (
    <div className="container mx-auto max-w-4xl py-8 px-6">
      <ModelRoleSettings />
    </div>
  );
}

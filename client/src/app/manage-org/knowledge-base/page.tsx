"use client";

import { MemorySettings } from "@/components/MemorySettings";

// Legacy route — Knowledge Base was replaced by Memory (Settings → Memory tab).
export default function LegacyKnowledgeBasePage() {
  return (
    <div className="container mx-auto max-w-4xl py-8 px-6">
      <MemorySettings />
    </div>
  );
}

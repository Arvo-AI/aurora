"use client";

import { PlatformMemoryCard } from "@/components/PlatformMemoryCard";

interface TeamsMemoryCardProps {
  readonly canWrite: boolean;
}

export function TeamsMemoryCard({ canWrite }: TeamsMemoryCardProps) {
  return (
    <PlatformMemoryCard
      platform="teams"
      displayName="Microsoft Teams"
      canWrite={canWrite}
      description={
        <>
          How Aurora behaves in Microsoft Teams — tone, when it speaks, and which channels
          own which services. Aurora reads this before every Teams reply. Same{" "}
          <span className="font-medium">Context → Microsoft Teams</span> entry as in Settings →
          Memory.
        </>
      }
    />
  );
}

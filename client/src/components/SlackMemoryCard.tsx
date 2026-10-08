"use client";

import { PlatformMemoryCard } from "@/components/PlatformMemoryCard";

interface SlackMemoryCardProps {
  // Editing is gated on the same role check the rest of the manage page uses.
  readonly canWrite: boolean;
}

// The Slack memory is an ordinary `context` / "Slack" memory entry, surfaced on
// the Slack manage page via the platform-generic card.
export function SlackMemoryCard({ canWrite }: SlackMemoryCardProps) {
  return (
    <PlatformMemoryCard
      platform="slack"
      displayName="Slack"
      canWrite={canWrite}
      description={
        <>
          How Aurora behaves in Slack — its tone, when it speaks up, the message
          format, and which teams/channels own which services. Aurora reads this
          before every Slack reply and appends to it as your team states
          preferences, so editing it here changes how Aurora shows up in Slack.
          This is the same <span className="font-medium">Context → Slack</span>{" "}
          memory entry from Settings → Memory, surfaced here so you don&apos;t have
          to go looking for it.
        </>
      }
    />
  );
}

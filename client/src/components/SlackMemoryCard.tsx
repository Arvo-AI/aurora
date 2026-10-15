"use client";

import { PolicyMemoryCard } from "@/components/PolicyMemoryCard";

interface SlackMemoryCardProps {
  readonly canWrite: boolean;
}

export function SlackMemoryCard({ canWrite }: SlackMemoryCardProps) {
  return (
    <PolicyMemoryCard
      canWrite={canWrite}
      endpoint="slack"
      name="Slack"
      placeholder="Describe how Aurora should behave in Slack (markdown)."
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

"use client";

import { useState } from "react";
import { useQuery } from "@/lib/query";

export type Level = "off" | "listen" | "sure" | "free";
type ReportStatus = "new" | "verifying" | "confirmed" | "answered" | "dismissed" | "merged" | "skipped";

export interface SlackReport {
  id: string;
  channel: string;
  said_at: string;
  kind: "problem" | "question";
  asset: string | null;
  claim: string;
  status: ReportStatus;
  status_detail: string | null;
  incident_id: string | null;
  reply: string | null;
}

interface Channel {
  channel_id: string;
  name: string;
  is_private: boolean;
  chosen: Level | null;
  level: Level;
}

export interface AlertingResponse {
  enabled: boolean;
  level: Level;
  channels: Channel[];
  reports: SlackReport[];
  can_set_level: boolean;
}

/** One request, one shape: the slider in Settings and the record on the Checks
 *  page read the same cached answer. */
export const ALERTING_KEY = "/api/detection/reports";

export const alertingFetcher = async (key: string, signal: AbortSignal): Promise<AlertingResponse> => {
  const res = await fetch(key, { credentials: "include", signal });
  if (!res.ok) throw new Error(`Failed to load dynamic alerting: ${res.status}`);
  const data = await res.json();
  return {
    enabled: Boolean(data.enabled),
    level: data.level || "off",
    channels: data.channels || [],
    reports: data.reports || [],
    can_set_level: Boolean(data.can_set_level),
  };
};

/** Quietest first. Each stop does what the one before it does, and more. */
const STOPS: readonly { key: Level; name: string; means: string }[] = [
  { key: "off", name: "Off", means: "Aurora does not read your channels. It still answers when someone @mentions it." },
  { key: "listen", name: "Listening", means: "Aurora reads your public channels and looks into problems people mention. A real one becomes an incident. It never posts unasked." },
  { key: "sure", name: "Speaks when sure", means: "Aurora also replies in the thread, only once it has confirmed that something is broken." },
  { key: "free", name: "Speaks freely", means: "Aurora also answers questions nobody answered, and says what it checked when it found nothing wrong." },
];

const PROBLEM_REPLY =
  "checkout has 0 of 3 pods ready since 14:02. The 14:01 deploy is crash-looping on a missing variable. I opened an incident and I am investigating.";

function Said({ who, children }: { readonly who: string; readonly children: React.ReactNode }) {
  return (
    <p className="text-sm">
      <span className="font-medium">{who}</span>{" "}
      <span className="text-muted-foreground">{children}</span>
    </p>
  );
}

function Outcome({ spoken, children }: { readonly spoken?: boolean; readonly children: React.ReactNode }) {
  if (!spoken) return <p className="ml-4 mt-1 text-xs text-muted-foreground">{children}</p>;
  return (
    <div className="ml-4 mt-1.5 border-l-2 border-foreground/40 pl-3">
      <p className="text-xs font-medium">Aurora, in the thread</p>
      <p className="text-sm text-muted-foreground">{children}</p>
    </div>
  );
}

/** The same two messages in a channel, and what Aurora does about each at *level*. */
function Preview({ level }: { readonly level: Level }) {
  const problem: Record<Level, React.ReactNode> = {
    off: <Outcome>Aurora does not see this.</Outcome>,
    listen: <Outcome>Aurora checks checkout without posting. It is down, so an incident opens and the on-call is told the usual way.</Outcome>,
    sure: <Outcome spoken>{PROBLEM_REPLY}</Outcome>,
    free: <Outcome spoken>{PROBLEM_REPLY}</Outcome>,
  };
  const question: Record<Level, React.ReactNode> = {
    off: <Outcome>Aurora does not see this.</Outcome>,
    listen: <Outcome>Not a problem, so Aurora does nothing.</Outcome>,
    sure: <Outcome>Not a confirmed problem, so Aurora stays quiet.</Outcome>,
    free: <Outcome spoken>It runs on prod-east, in the shop namespace.</Outcome>,
  };
  return (
    <div className="rounded-lg border border-border px-4 py-3 space-y-3">
      <div>
        <Said who="Maya">is checkout timing out for anyone else?</Said>
        {problem[level]}
      </div>
      <div>
        <Said who="Sam">which cluster does checkout run on again?</Said>
        {question[level]}
      </div>
    </div>
  );
}

function setApart(channels: Channel[]): string | null {
  const named = channels.filter((c) => c.chosen !== null);
  if (named.length === 0) return null;
  return named
    .map((c) => `#${c.name}: ${STOPS.find((s) => s.key === c.chosen)?.name.toLowerCase()}`)
    .join(", ");
}

/** The one control for what Aurora does unasked in Slack: a slider, quiet to noisy.
 *  Renders nothing when the install has it off or the org has no Slack. */
export default function DynamicAlerting() {
  const { data, mutate } = useQuery<AlertingResponse>(ALERTING_KEY, alertingFetcher, {
    staleTime: 10_000,
    retryCount: 0,
  });
  // The stop under the thumb while it is being moved; null once saved.
  const [held, setHeld] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  if (!data?.enabled) return null;

  const saved = STOPS.findIndex((s) => s.key === data.level);
  const position = held ?? saved;
  const stop = STOPS[position];
  const locked = !data.can_set_level || saving;

  const save = async (next: number) => {
    if (next === saved) {
      setHeld(null);
      return;
    }
    setHeld(next);
    setSaving(true);
    setFailed(false);
    let ok = false;
    try {
      const res = await fetch("/api/detection/chat-level", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: STOPS[next].key }),
      });
      ok = res.ok;
    } catch {
      ok = false;
    }
    await mutate();
    setSaving(false);
    setFailed(!ok);
    setHeld(null);
  };

  const reading = data.channels.filter((c) => c.level !== "off").length;
  const apart = setApart(data.channels);

  return (
    <section aria-labelledby="dynamic-alerting">
      <div className="flex items-baseline justify-between mb-3">
        <h3 id="dynamic-alerting" className="text-sm font-medium">Dynamic alerting</h3>
        <span className="text-xs text-muted-foreground">{stop.name}</span>
      </div>

      <input
        type="range"
        min={0}
        max={STOPS.length - 1}
        step={1}
        value={position}
        disabled={locked}
        aria-label="How much Aurora does in Slack without being asked"
        aria-valuetext={stop.name}
        onChange={(e) => setHeld(Number(e.target.value))}
        onPointerUp={() => held !== null && save(held)}
        onKeyUp={() => held !== null && save(held)}
        onBlur={() => held !== null && save(held)}
        className="w-full accent-foreground cursor-pointer disabled:cursor-not-allowed"
      />
      <div className="flex justify-between text-[11px] text-muted-foreground mt-1">
        <span>Quiet</span>
        <span>Noisy</span>
      </div>

      <p className="text-sm text-muted-foreground mt-3">{stop.means}</p>

      <div className="mt-3">
        <Preview level={stop.key} />
      </div>

      <p className="text-xs text-muted-foreground mt-3">
        {stop.key === "off"
          ? "Nothing is read."
          : `Reading ${reading} of ${data.channels.length} channels Aurora is in. Private channels are off unless Aurora was told otherwise.`}
        {apart && ` Set apart: ${apart}.`}
        {" "}To treat one channel differently, tell Aurora in Slack, like &ldquo;@Aurora be quieter in #payments&rdquo;. It keeps that in its memory.
      </p>
      {!data.can_set_level && <p className="text-xs text-muted-foreground mt-2">Only an admin can move this.</p>}
      {failed && <p className="text-xs text-red-500 mt-2">That did not save. Try again.</p>}
    </section>
  );
}

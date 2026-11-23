"use client";

import { useRef, useState } from "react";
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
  { key: "off", name: "Off", means: "Aurora stays out of your channels." },
  { key: "listen", name: "Listening", means: "Reads, investigates, opens incidents. Never posts." },
  { key: "sure", name: "Speaks when sure", means: "Replies in the thread once it has proof." },
  { key: "free", name: "Speaks freely", means: "Also answers questions nobody answered." },
];
const LAST = STOPS.length - 1;
const BARS = 64;
// How close to a stop, as a share of the track, the thumb starts being pulled to it.
const PULL = 0.07;
// Half the thumb: its centre travels between these insets, so it never leaves the track.
const INSET = 18;
const place = (at: number) => `calc(${INSET}px + (100% - ${INSET * 2}px) * ${at})`;

const nearest = (at: number) => Math.min(LAST, Math.max(0, Math.round(at * LAST)));

/** Where the thumb sits for a pointer at *raw* (0..1 across the track): held back
 *  past either end, and drawn towards a stop when close to one. */
function feel(raw: number): number {
  if (raw < 0) return raw * 0.18;
  if (raw > 1) return 1 + (raw - 1) * 0.18;
  const stop = nearest(raw) / LAST;
  const off = raw - stop;
  return Math.abs(off) < PULL ? stop + off * 0.3 : raw;
}

/** The track: quiet on the left, loud on the right, drawn as the noise itself. */
function Track({ at, moving }: { readonly at: number; readonly moving: boolean }) {
  return (
    <div className="absolute inset-x-0 bottom-0 top-0 flex items-end gap-[3px]" aria-hidden>
      {Array.from({ length: BARS }, (_, i) => {
        const share = (i + 0.5) / BARS;
        const lit = share <= at + 0.0001;
        // A slow swell, so the loud end reads as louder and not only taller.
        const height = 8 + 92 * Math.pow(share, 1.7) * (0.82 + 0.18 * Math.sin(i * 1.9));
        return (
          <span
            key={i}
            className={`flex-1 rounded-full ${moving ? "" : "transition-colors duration-300 motion-reduce:transition-none"} ${
              lit ? "bg-foreground" : "bg-foreground/15"
            }`}
            style={{ height: `${height}%` }}
          />
        );
      })}
    </div>
  );
}

function Reveal({ show, children }: { readonly show: boolean; readonly children: React.ReactNode }) {
  return (
    <div
      className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none ${
        show ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"
      }`}
    >
      <div className="overflow-hidden">{children}</div>
    </div>
  );
}

function Line({ who, aurora, children }: { readonly who: string; readonly aurora?: boolean; readonly children: React.ReactNode }) {
  return (
    <div className={`flex gap-2.5 ${aurora ? "pl-8 pt-2" : ""}`}>
      <span
        className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold ${
          aurora ? "bg-foreground text-background" : "bg-muted text-muted-foreground"
        }`}
      >
        {who[0]}
      </span>
      <p className="min-w-0 text-sm leading-relaxed">
        <span className="font-medium">{who}</span>{" "}
        <span className={aurora ? "" : "text-muted-foreground"}>{children}</span>
      </p>
    </div>
  );
}

/** The same two messages, and what Aurora does about each at stop *index*. */
function Channel({ index }: { readonly index: number }) {
  return (
    <div className="space-y-4" aria-live="polite">
      <div>
        <Line who="Maya">is checkout timing out for anyone else?</Line>
        <Reveal show={index === 1}>
          <p className="pl-[34px] pt-1.5 text-xs text-muted-foreground">Incident opened. Nothing posted here.</p>
        </Reveal>
        <Reveal show={index >= 2}>
          <Line who="Aurora" aurora>
            checkout has 0 of 3 pods ready since 14:02. I opened an incident.
          </Line>
        </Reveal>
      </div>
      <div>
        <Line who="Sam">which cluster does checkout run on again?</Line>
        <Reveal show={index >= 3}>
          <Line who="Aurora" aurora>
            prod-east, in the shop namespace.
          </Line>
        </Reveal>
      </div>
    </div>
  );
}

/** The one control for what Aurora does unasked in Slack: a slider, quiet to noisy.
 *  Renders nothing when the install has it off or the org has no Slack. */
export default function DynamicAlerting() {
  const { data, mutate } = useQuery<AlertingResponse>(ALERTING_KEY, alertingFetcher, {
    staleTime: 10_000,
    retryCount: 0,
  });
  const track = useRef<HTMLDivElement>(null);
  // Where the thumb is while a hand is on it (0..1, may run a little past an end).
  const [drag, setDrag] = useState<number | null>(null);
  // The stop just chosen, shown until the save has been read back.
  const [chosen, setChosen] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);
  const ticked = useRef<number | null>(null);

  if (!data?.enabled) return null;

  const saved = STOPS.findIndex((s) => s.key === data.level);
  const index = drag === null ? chosen ?? saved : nearest(drag);
  const at = drag ?? index / LAST;
  const stop = STOPS[index];
  const locked = !data.can_set_level;

  const save = async (next: number) => {
    if (next === saved) return;
    setChosen(next);
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
    setFailed(!ok);
    setChosen(null);
  };

  const follow = (clientX: number) => {
    const box = track.current?.getBoundingClientRect();
    if (!box) return;
    const next = feel((clientX - box.left - INSET) / (box.width - INSET * 2));
    const over = nearest(next);
    if (ticked.current !== over) {
      // A click under the finger when a stop is crossed, where the device has one.
      if (ticked.current !== null) navigator.vibrate?.(6);
      ticked.current = over;
    }
    setDrag(next);
  };

  const release = () => {
    if (drag === null) return;
    const next = nearest(drag);
    setDrag(null);
    ticked.current = null;
    save(next);
  };

  const onKey = (e: React.KeyboardEvent) => {
    const moves: Record<string, number> = {
      ArrowLeft: index - 1, ArrowDown: index - 1, ArrowRight: index + 1, ArrowUp: index + 1, Home: 0, End: LAST,
    };
    if (!(e.key in moves)) return;
    e.preventDefault();
    save(Math.min(LAST, Math.max(0, moves[e.key])));
  };

  const reading = data.channels.filter((c) => c.level !== "off").length;

  return (
    <section aria-labelledby="dynamic-alerting">
      <h3 id="dynamic-alerting" className="text-sm font-medium">Dynamic alerting</h3>

      <div className="mt-5 flex items-end justify-between gap-6">
        <p key={stop.key} className="text-3xl font-semibold tracking-tight animate-in fade-in slide-in-from-bottom-1 duration-200">
          {stop.name}
        </p>
        <p className="max-w-[16rem] pb-1 text-right text-sm text-muted-foreground">{stop.means}</p>
      </div>

      <div
        ref={track}
        role="slider"
        tabIndex={locked ? -1 : 0}
        aria-label="How much Aurora does in Slack without being asked"
        aria-valuemin={0}
        aria-valuemax={LAST}
        aria-valuenow={index}
        aria-valuetext={stop.name}
        aria-disabled={locked}
        onKeyDown={locked ? undefined : onKey}
        onPointerDown={locked ? undefined : (e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          e.currentTarget.focus();
          follow(e.clientX);
        }}
        onPointerMove={(e) => drag !== null && follow(e.clientX)}
        onPointerUp={release}
        onPointerCancel={release}
        className={`group relative mt-5 h-20 touch-none select-none rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-foreground/40 focus-visible:ring-offset-4 focus-visible:ring-offset-background ${
          locked ? "cursor-not-allowed opacity-60" : drag === null ? "cursor-grab" : "cursor-grabbing"
        }`}
      >
        <Track at={at} moving={drag !== null} />
        <div
          className={`absolute top-1/2 h-9 w-9 -translate-x-1/2 -translate-y-1/2 rounded-full border border-black/10 bg-white shadow-[0_2px_10px_rgba(0,0,0,0.45)] motion-reduce:transition-none ${
            drag === null
              ? "transition-[left,transform] duration-500 ease-[cubic-bezier(0.2,1.5,0.4,1)] group-hover:scale-105"
              : "scale-110 transition-transform duration-150"
          }`}
          style={{ left: place(at) }}
        >
          <span className="absolute inset-0 m-auto h-3 w-3 rounded-full bg-black/80" />
        </div>
      </div>

      <div className="relative mt-3 h-4">
        {STOPS.map((s, i) => {
          const edge = i === 0 ? "left-0" : i === LAST ? "right-0" : "-translate-x-1/2";
          return (
            <button
              key={s.key}
              type="button"
              tabIndex={-1}
              disabled={locked}
              onClick={() => save(i)}
              style={i === 0 || i === LAST ? undefined : { left: place(i / LAST) }}
              className={`absolute top-0 whitespace-nowrap text-xs transition-colors ${edge} ${
                i === index ? "text-foreground" : "text-muted-foreground/60 hover:text-muted-foreground"
              }`}
            >
              {s.name}
            </button>
          );
        })}
      </div>

      <div className="mt-6 rounded-xl border border-border bg-muted/30 px-4 py-4">
        <p className="mb-3 text-xs text-muted-foreground"># payments</p>
        <Channel index={index} />
      </div>

      <p className="mt-3 text-xs text-muted-foreground">
        {index === 0 ? "Reading no channels." : `Reading ${reading} of ${data.channels.length} channels.`}{" "}
        For one channel, tell Aurora: &ldquo;@Aurora be quieter in #payments&rdquo;.
        {locked && " Only an admin can move this."}
      </p>
      {failed && <p className="mt-2 text-xs text-red-500">That did not save. Try again.</p>}
    </section>
  );
}

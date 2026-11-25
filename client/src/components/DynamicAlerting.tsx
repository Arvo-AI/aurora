"use client";

import { useEffect, useRef, useState } from "react";
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
// How close to a stop, as a share of the bar, the fill starts being pulled to it.
const PULL = 0.07;
// How far past an end the bar gives, as a share of how far the hand went.
const GIVE = 0.18;
// A release faster than this (bar widths per millisecond) carries on to the next stop.
const FLICK = 0.0011;

const SPRING = "ease-[cubic-bezier(0.2,1.5,0.4,1)]";

const nearest = (at: number) => Math.min(LAST, Math.max(0, Math.round(at * LAST)));

/** How full the bar is for a pointer at *raw* (0..1 across the bar): held back
 *  past either end, and drawn towards a stop when close to one. */
function feel(raw: number): number {
  if (raw < 0) return raw * GIVE;
  if (raw > 1) return 1 + (raw - 1) * GIVE;
  const stop = nearest(raw) / LAST;
  const off = raw - stop;
  return Math.abs(off) < PULL ? stop + off * 0.3 : raw;
}

/** A speaker that gets louder with the stop: crossed out at off, one more wave at each stop after. */
function Loudness({ index }: { readonly index: number }) {
  const wave = (from: number) =>
    `origin-left transition-[opacity,transform] duration-300 ${SPRING} motion-reduce:transition-none ${
      index >= from ? "scale-100 opacity-100" : "scale-50 opacity-0"
    }`;
  return (
    <svg
      aria-hidden
      viewBox="0 0 32 24"
      className="pointer-events-none absolute right-5 top-1/2 hidden h-6 w-8 -translate-y-1/2 text-white mix-blend-difference sm:block"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 9h3.5L11 5v14l-4.5-4H3z" fill="currentColor" />
      <path d="M15 9.5a4 4 0 0 1 0 5" className={wave(1)} />
      <path d="M18.5 7a8 8 0 0 1 0 10" className={wave(2)} />
      <path d="M22 4.5a12 12 0 0 1 0 15" className={wave(3)} />
      <path d="M15 9l6 6m0-6l-6 6" className={`transition-opacity duration-200 ${index === 0 ? "opacity-100" : "opacity-0"}`} />
    </svg>
  );
}

function Reveal({ show, children }: { readonly show: boolean; readonly children: React.ReactNode }) {
  return (
    <div
      aria-hidden={!show}
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

/** The one control for what Aurora does unasked in Slack: a bar that fills, quiet to noisy.
 *  No thumb; the whole bar is the handle.
 *  Renders nothing when the install has it off or the org has no Slack. */
export default function DynamicAlerting() {
  const { data, mutate } = useQuery<AlertingResponse>(ALERTING_KEY, alertingFetcher, {
    staleTime: 10_000,
    retryCount: 0,
  });
  const track = useRef<HTMLDivElement>(null);
  // How full the bar is while a hand is on it (0..1, may run a little past an end).
  const [drag, setDrag] = useState<number | null>(null);
  // The stop just chosen, shown until the save has been read back.
  const [chosen, setChosen] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);
  const ticked = useRef<number | null>(null);
  const last = useRef<{ at: number; when: number; speed: number } | null>(null);
  const sound = useRef<AudioContext | null>(null);
  // Saves go out one at a time, and the last stop chosen is the last one sent.
  const sending = useRef(false);
  const wanted = useRef<number | null>(null);

  useEffect(() => () => {
    void sound.current?.close();
  }, []);

  // A soft click, a little higher at each louder stop. Silent where sound is not allowed.
  const click = (stopIndex: number) => {
    navigator.vibrate?.(6);
    try {
      sound.current ??= new AudioContext();
      const ctx = sound.current;
      const tone = ctx.createOscillator();
      const volume = ctx.createGain();
      tone.type = "sine";
      tone.frequency.value = 330 * Math.pow(2, stopIndex / 4);
      volume.gain.setValueAtTime(0.05, ctx.currentTime);
      volume.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.07);
      tone.connect(volume).connect(ctx.destination);
      tone.start();
      tone.stop(ctx.currentTime + 0.08);
    } catch {
      // No sound on this device; the bar works the same.
    }
  };

  if (!data?.enabled) return null;

  const saved = STOPS.findIndex((s) => s.key === data.level);
  const index = drag === null ? chosen ?? saved : nearest(drag);
  const at = drag ?? index / LAST;
  const stop = STOPS[index];
  const locked = !data.can_set_level;

  const send = async (next: number) => {
    try {
      const res = await fetch("/api/detection/chat-level", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: STOPS[next].key }),
      });
      return res.ok;
    } catch {
      return false;
    }
  };

  // Sends *first*, then whatever was chosen meanwhile, until nothing is waiting.
  // A stop chosen while the answer is being read back is sent too.
  const drain = async (first: number): Promise<boolean> => {
    wanted.current = null;
    const ok = await send(first);
    if (wanted.current === null) await mutate();
    const waiting = wanted.current;
    return waiting === null ? ok : drain(waiting);
  };

  const save = async (next: number) => {
    if (next === saved && !sending.current) return;
    setChosen(next);
    setFailed(false);
    wanted.current = next;
    if (sending.current) return;
    sending.current = true;
    const ok = await drain(next);
    sending.current = false;
    setFailed(!ok);
    setChosen(null);
  };

  const follow = (clientX: number) => {
    const box = track.current?.getBoundingClientRect();
    if (!box) return;
    const next = feel((clientX - box.left) / box.width);
    const over = nearest(next);
    if (ticked.current !== over) {
      if (ticked.current !== null) click(over);
      ticked.current = over;
    }
    const now = performance.now();
    const before = last.current;
    last.current = {
      at: next,
      when: now,
      speed: before && now > before.when ? (next - before.at) / (now - before.when) : 0,
    };
    setDrag(next);
  };

  const release = () => {
    if (drag === null) return;
    // Thrown, the bar carries on to the next stop the way it was going.
    const speed = last.current?.speed ?? 0;
    let next = nearest(drag);
    if (speed > FLICK) next = Math.min(LAST, Math.ceil(drag * LAST));
    if (speed < -FLICK) next = Math.max(0, Math.floor(drag * LAST));
    if (next !== ticked.current) click(next);
    setDrag(null);
    ticked.current = null;
    last.current = null;
    void save(next);
  };

  const onKey = (e: React.KeyboardEvent) => {
    const moves: Record<string, number> = {
      ArrowLeft: index - 1, ArrowDown: index - 1, ArrowRight: index + 1, ArrowUp: index + 1, Home: 0, End: LAST,
    };
    if (!(e.key in moves)) return;
    e.preventDefault();
    const next = Math.min(LAST, Math.max(0, moves[e.key]));
    if (next !== index) click(next);
    void save(next);
  };

  const grip = drag === null ? "cursor-grab" : "cursor-grabbing";
  const full = Math.min(1, Math.max(0, at));
  const past = at - full;
  const reading = data.channels.filter((c) => c.level !== "off").length;

  return (
    <section aria-labelledby="dynamic-alerting">
      <h3 id="dynamic-alerting" className="text-sm font-medium">Dynamic alerting</h3>

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
        style={{
          // Pulled past an end, the bar stretches towards the hand and thins, then springs back.
          // Held, it swells a little under the hand.
          transform: `scale(${1 + Math.abs(past) * 0.5}, ${(drag === null ? 1 : 1.06) - Math.abs(past) * 0.6})`,
          transformOrigin: past < 0 ? "right center" : "left center",
        }}
        className={`relative isolate mt-4 h-16 touch-none select-none overflow-hidden rounded-2xl bg-muted outline-none focus-visible:ring-2 focus-visible:ring-foreground/40 focus-visible:ring-offset-4 focus-visible:ring-offset-background motion-reduce:transition-none ${
drag === null ? `transition-transform duration-500 ${SPRING}` : "transition-transform duration-150"
        } ${locked ? "cursor-not-allowed opacity-60" : grip}`}
      >
        <div
          className={`absolute inset-y-0 left-0 bg-foreground motion-reduce:transition-none ${
            drag === null ? `transition-[width] duration-500 ${SPRING}` : ""
          }`}
          style={{ width: `${full * 100}%` }}
        />
        {STOPS.slice(1, LAST).map((s, i) => (
          <span
            key={s.key}
            aria-hidden
            className="absolute top-1/2 h-2 w-0.5 -translate-y-1/2 rounded-full bg-white/40 mix-blend-difference"
            style={{ left: `${((i + 1) / LAST) * 100}%` }}
          />
        ))}
        <p
          key={stop.key}
          className="pointer-events-none absolute inset-y-0 left-5 flex items-center whitespace-nowrap text-lg font-semibold tracking-tight text-white mix-blend-difference animate-in fade-in zoom-in-90 slide-in-from-left-2 duration-200"
        >
          {stop.name}
        </p>
        <Loudness index={index} />
      </div>

      <p className="mt-3 text-sm text-muted-foreground">{stop.means}</p>

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

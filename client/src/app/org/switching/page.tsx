"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";

interface Step {
  label: string;
  run: () => Promise<void>;
}

async function refreshSession(): Promise<void> {
  const csrfResp = await fetch("/api/auth/csrf");
  if (!csrfResp.ok) throw new Error("csrf");
  const { csrfToken } = await csrfResp.json();

  const res = await fetch("/api/auth/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ csrfToken }),
  });
  if (!res.ok) throw new Error("refresh");

  const session = await res.json();
  if (!session?.orgId) throw new Error("missing org");
}

async function verifyOrg(): Promise<void> {
  const res = await fetch("/api/orgs/current");
  if (!res.ok) throw new Error("org");
}

async function prefetchDashboard(): Promise<void> {
  await Promise.allSettled(
    ["/api/incidents", "/api/orgs/stats", "/api/llm-usage/models", "/chat_api/sessions"].map(
      (url) => fetch(url),
    ),
  );
}

const STEPS: Step[] = [
  { label: "Refreshing your session", run: refreshSession },
  { label: "Verifying organization access", run: verifyOrg },
  { label: "Loading your workspace", run: prefetchDashboard },
];

export default function OrgSwitchingPage() {
  const started = useRef(false);
  const [current, setCurrent] = useState(0);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    (async () => {
      for (let i = 0; i < STEPS.length; i++) {
        setCurrent(i);
        try {
          await STEPS[i].run();
        } catch {
          if (i === 0) {
            setError("Session refresh failed. Redirecting…");
            await new Promise((r) => setTimeout(r, 1500));
            window.location.replace("/sign-in");
            return;
          }
        }
      }

      setDone(true);
      await new Promise((r) => setTimeout(r, 500));
      window.location.replace("/");
    })();
  }, []);

  const progress = done ? 100 : ((current + 0.5) / STEPS.length) * 100;

  return (
    <div className="fixed inset-0 flex flex-col items-center justify-center bg-[#09090b] overflow-hidden select-none">
      {/* Ambient glow */}
      <div
        className="pointer-events-none absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] rounded-full opacity-[0.07]"
        style={{
          background:
            "radial-gradient(circle, rgba(56,189,248,0.8) 0%, rgba(139,92,246,0.5) 40%, transparent 70%)",
        }}
      />

      {/* Content */}
      <div
        className={`relative z-10 flex flex-col items-center w-full max-w-xs px-6 transition-all duration-700 ${
          mounted ? "opacity-100 translate-y-0" : "opacity-0 translate-y-4"
        }`}
      >
        {/* Logo + wordmark */}
        <div className="flex items-center gap-2.5 mb-10">
          <Image
            src="/arvologotransparent.png"
            alt="Aurora"
            width={36}
            height={36}
            className="drop-shadow-[0_0_12px_rgba(56,189,248,0.25)]"
            priority
          />
          <span className="text-[22px] font-bold tracking-tight text-white">
            Aurora
          </span>
        </div>

        {/* Steps */}
        <div className="w-full space-y-2.5 mb-8">
          {STEPS.map(({ label }, i) => {
            const isActive = current === i && !done && !error;
            const isComplete = current > i || done;

            return (
              <div
                key={label}
                className={`flex items-center gap-3 px-3.5 py-2.5 rounded-lg transition-all duration-500 ${
                  isComplete
                    ? "opacity-100"
                    : isActive
                      ? "opacity-100"
                      : "opacity-30"
                }`}
              >
                {/* Indicator */}
                <div className="relative flex-shrink-0 w-5 h-5 flex items-center justify-center">
                  {isComplete ? (
                    <svg
                      className="w-5 h-5 text-emerald-400 animate-[scaleIn_0.3s_ease-out]"
                      viewBox="0 0 20 20"
                      fill="currentColor"
                    >
                      <path
                        fillRule="evenodd"
                        d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.857-9.809a.75.75 0 00-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 10-1.06 1.061l2.5 2.5a.75.75 0 001.137-.089l4-5.5z"
                        clipRule="evenodd"
                      />
                    </svg>
                  ) : isActive ? (
                    <div className="w-4 h-4 rounded-full border-2 border-sky-400/80 border-t-transparent animate-spin" />
                  ) : (
                    <div className="w-3 h-3 rounded-full border border-zinc-600" />
                  )}
                </div>

                {/* Label */}
                <span
                  className={`text-[13px] font-medium transition-colors duration-500 ${
                    isComplete
                      ? "text-emerald-400"
                      : isActive
                        ? "text-zinc-200"
                        : "text-zinc-500"
                  }`}
                >
                  {label}
                </span>
              </div>
            );
          })}
        </div>

        {/* Progress bar */}
        <div className="w-full h-[3px] rounded-full bg-zinc-800/80 overflow-hidden">
          <div
            className="h-full rounded-full transition-all duration-700 ease-out"
            style={{
              width: `${progress}%`,
              background: done
                ? "rgb(52,211,153)"
                : "linear-gradient(90deg, rgb(56,189,248), rgb(139,92,246))",
            }}
          />
        </div>

        {/* Status text */}
        <p
          className={`mt-5 text-xs transition-all duration-500 ${
            error
              ? "text-red-400"
              : done
                ? "text-emerald-400/70"
                : "text-zinc-500"
          }`}
        >
          {error ?? (done ? "Ready" : "Setting things up…")}
        </p>
      </div>

      <style jsx>{`
        @keyframes scaleIn {
          0% {
            transform: scale(0);
            opacity: 0;
          }
          100% {
            transform: scale(1);
            opacity: 1;
          }
        }
      `}</style>
    </div>
  );
}

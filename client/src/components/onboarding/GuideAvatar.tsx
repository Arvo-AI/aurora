"use client"

import type { SetupGuide } from "@/hooks/use-setup-guide"

// A real face (or initials) inside a thin ring of the aurora gradient, so the
// person reads as part of Aurora rather than a support widget.
export default function GuideAvatar({ guide, size = 40 }: Readonly<{ guide: SetupGuide; size?: number }>) {
  const initials = guide.name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("")
  return (
    <span
      className="relative inline-flex shrink-0 rounded-full p-[1.5px]"
      style={{ width: size, height: size, background: "linear-gradient(135deg, #26e680, #1a99cc 55%, #9926b3)" }}
      aria-hidden="true"
    >
      <span className="flex h-full w-full items-center justify-center overflow-hidden rounded-full bg-[#0d0f10]">
        {guide.avatar_url ? (
          // eslint-disable-next-line @next/next/no-img-element -- avatar host comes from config, not next.config images
          <img src={guide.avatar_url} alt="" className="h-full w-full object-cover" />
        ) : (
          <span className="font-medium text-white/85" style={{ fontSize: size * 0.36 }}>{initials}</span>
        )}
      </span>
    </span>
  )
}

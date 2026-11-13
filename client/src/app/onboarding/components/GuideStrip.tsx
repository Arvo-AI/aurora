"use client"

import GuideAvatar from "@/components/onboarding/GuideAvatar"
import type { SetupGuide } from "@/hooks/use-setup-guide"

export default function GuideStrip({ guide, team, done }: Readonly<{ guide: SetupGuide; team: string; done: boolean }>) {
  const line = done
    ? `${guide.first_name} is still your contact on the ${team} team. If anything in your first investigations looks off, tell ${guide.first_name}.`
    : `${guide.first_name} from the ${team} team is here to help you set up. Book 15 minutes and do it together, or send a question.`
  return (
    <div className="flex gap-4 rounded-2xl border border-white/[0.08] bg-white/[0.03] px-5 py-5 backdrop-blur-md">
      <GuideAvatar guide={guide} size={44} />
      <div className="min-w-0 flex-1">
        <p className="text-[15px] font-medium text-white">
          {guide.name}
          {guide.title && <span className="ml-2 text-[13px] font-normal text-white/40">{guide.title}</span>}
        </p>
        <p className="mt-1 max-w-[52ch] text-[13px] leading-relaxed text-white/50">{line}</p>
        {(guide.booking_url || guide.email) && (
          <div className="mt-3.5 flex flex-wrap items-center gap-2">
            {guide.booking_url && (
              <a
                href={guide.booking_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center rounded-lg bg-white px-4 py-2 text-sm font-medium text-black transition-colors hover:bg-white/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 focus-visible:ring-offset-2 focus-visible:ring-offset-black"
              >
                Book 15 minutes
              </a>
            )}
            {guide.email && (
              <a
                href={`mailto:${guide.email}?subject=${encodeURIComponent("Aurora setup")}`}
                className="inline-flex items-center rounded-lg border border-white/15 px-4 py-2 text-sm font-medium text-white/85 transition-colors hover:bg-white/[0.06] hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30"
              >
                Email {guide.first_name}
              </a>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

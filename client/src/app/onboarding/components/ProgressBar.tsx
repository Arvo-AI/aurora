"use client"

import { motion } from "framer-motion"

interface ProgressBarProps {
  step: number
  totalVisible: number
}

export default function ProgressBar({ step, totalVisible }: Readonly<ProgressBarProps>) {
  const clamped = Math.min(step, totalVisible)
  const percent = (clamped / totalVisible) * 100

  return (
    <div>
      <div className="w-full h-[3px] bg-white/[0.08] rounded-full overflow-hidden">
        <motion.div
          className="h-full bg-[#3fa266] rounded-full"
          initial={false}
          animate={{ width: `${percent}%` }}
          transition={{ duration: 0.5, ease: [0.4, 0, 0.2, 1] }}
        />
      </div>
      <p className="text-right text-[11px] text-[#777] mt-1.5">
        Step {clamped} of {totalVisible}
      </p>
    </div>
  )
}

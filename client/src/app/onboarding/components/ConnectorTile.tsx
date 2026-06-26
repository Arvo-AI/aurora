"use client"

import Image from "next/image"
import { motion, AnimatePresence } from "framer-motion"
import { Check, Info } from "lucide-react"
import { useState, useRef, useEffect } from "react"
import type { ConnectorConfig } from "@/components/connectors/types"

interface ConnectorTileProps {
  connector: ConnectorConfig
  selected: boolean
  onToggle: () => void
}

export default function ConnectorTile({ connector, selected, onToggle }: Readonly<ConnectorTileProps>) {
  const Icon = connector.icon
  const [showTooltip, setShowTooltip] = useState(false)
  const tooltipTimeout = useRef<ReturnType<typeof setTimeout> | null>(null)
  const tileRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    return () => {
      if (tooltipTimeout.current) clearTimeout(tooltipTimeout.current)
    }
  }, [])

  const handleInfoEnter = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (tooltipTimeout.current) clearTimeout(tooltipTimeout.current)
    setShowTooltip(true)
  }

  const handleInfoLeave = () => {
    tooltipTimeout.current = setTimeout(() => setShowTooltip(false), 150)
  }

  const renderIcon = () => {
    if (connector.iconPath) {
      return (
        <Image
          src={connector.iconPath}
          alt={connector.name}
          width={24}
          height={24}
          className="object-contain"
        />
      )
    }
    if (Icon) {
      return <Icon className={`w-5 h-5 ${connector.iconColor || "text-white"}`} />
    }
    return null
  }

  return (
    <div className="relative group/tile" ref={tileRef}>
      <button
        type="button"
        aria-pressed={selected}
        onClick={onToggle}
        className={`relative w-full flex items-start gap-3 p-4 rounded-lg border text-left transition-all duration-150 backdrop-blur-sm ${
          selected
            ? "border-[#3fa266]/60 bg-[#3fa266]/[0.1]"
            : "border-white/[0.1] bg-white/[0.05] hover:border-white/20 hover:bg-white/[0.08]"
        }`}
      >
        <AnimatePresence>
          {selected && (
            <motion.div
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              exit={{ scale: 0 }}
              transition={{ type: "spring", stiffness: 400, damping: 20 }}
              className="absolute top-2.5 right-2.5 w-5 h-5 rounded-full bg-[#3fa266] flex items-center justify-center"
            >
              <Check className="w-3 h-3 text-white" />
            </motion.div>
          )}
        </AnimatePresence>

        <div className="flex-shrink-0 w-9 h-9 rounded-lg flex items-center justify-center overflow-hidden bg-white/[0.06]">
          {renderIcon()}
        </div>

        <div className="flex-1 min-w-0 pr-5">
          <p className="text-sm font-medium text-white truncate">{connector.name}</p>
          <p className="text-xs text-[#888] mt-0.5 line-clamp-2">{connector.description}</p>
        </div>
      </button>

      {/* Info icon — only visible on tile hover */}
      <div
        className="absolute bottom-2 right-2 z-10 opacity-0 group-hover/tile:opacity-100 transition-opacity duration-150"
        onMouseEnter={handleInfoEnter}
        onMouseLeave={handleInfoLeave}
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            setShowTooltip(!showTooltip)
          }}
          className="w-5 h-5 rounded-full flex items-center justify-center text-[#555] hover:text-[#aaa] hover:bg-white/[0.08] transition-colors"
          aria-label={`More info about ${connector.name}`}
        >
          <Info className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Tooltip with full description */}
      <AnimatePresence>
        {showTooltip && (
          <motion.div
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 4 }}
            transition={{ duration: 0.15 }}
            onMouseEnter={handleInfoEnter}
            onMouseLeave={handleInfoLeave}
            className="absolute bottom-full left-0 right-0 mb-2 z-50 p-3 rounded-lg bg-[#1a1a1a] border border-white/[0.15] shadow-xl"
          >
            <p className="text-xs text-[#ccc] leading-relaxed">{connector.description}</p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

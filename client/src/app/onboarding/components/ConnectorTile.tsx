"use client"

import Image from "next/image"
import { motion, AnimatePresence } from "framer-motion"
import { Check } from "lucide-react"
import type { ConnectorConfig } from "@/components/connectors/types"

interface ConnectorTileProps {
  connector: ConnectorConfig
  selected: boolean
  onToggle: () => void
}

export default function ConnectorTile({ connector, selected, onToggle }: ConnectorTileProps) {
  const Icon = connector.icon

  return (
    <button
      onClick={onToggle}
      className={`relative flex items-start gap-3 p-4 rounded-lg border text-left transition-all duration-150 backdrop-blur-sm ${
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
        {connector.iconPath ? (
          <Image
            src={connector.iconPath}
            alt={connector.name}
            width={24}
            height={24}
            className="object-contain"
          />
        ) : Icon ? (
          <Icon className={`w-5 h-5 ${connector.iconColor || "text-white"}`} />
        ) : null}
      </div>

      <div className="flex-1 min-w-0 pr-5">
        <p className="text-sm font-medium text-white truncate">{connector.name}</p>
        <p className="text-xs text-[#888] mt-0.5 line-clamp-2">{connector.description}</p>
      </div>
    </button>
  )
}

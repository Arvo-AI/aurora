"use client"

import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { GitBranch, GitCommit, Upload, Check, Loader2, X } from "lucide-react"
import Image from "next/image"

interface GitHubCommitToolProps {
  repo?: string
  branch?: string
  defaultMessage?: string
  onCommit?: (message: string) => Promise<void>
  onPush?: () => Promise<void>
  onClose?: () => void
}

export function GitHubCommitTool({
  repo = "user/repository",
  branch = "main",
  defaultMessage = "Update files",
  onCommit,
  onPush,
  onClose,
}: GitHubCommitToolProps) {
  const [commitMessage, setCommitMessage] = useState(defaultMessage)
  const [isCommitting, setIsCommitting] = useState(false)
  const [isPushing, setIsPushing] = useState(false)
  const [isCommitted, setIsCommitted] = useState(false)
  const [isPushed, setIsPushed] = useState(false)

  const handleCommit = async () => {
    if (!commitMessage.trim()) return

    setIsCommitting(true)
    try {
      await onCommit?.(commitMessage)
      setIsCommitted(true)
    } catch (error) {
      console.error("Commit failed:", error)
    } finally {
      setIsCommitting(false)
    }
  }

  const handlePush = async () => {
    setIsPushing(true)
    try {
      await onPush?.()
      setIsPushed(true)
    } catch (error) {
      console.error("Push failed:", error)
    } finally {
      setIsPushing(false)
    }
  }

  return (
    <div className="flex items-center gap-3 bg-white dark:bg-black border border-gray-200 dark:border-gray-800/50 rounded-lg px-4 py-3 text-sm relative shadow-sm">
      {/* Close button */}
      {onClose && (
        <button
          onClick={onClose}
          className="absolute top-2 right-2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
        >
          <X className="w-3 h-3" />
        </button>
      )}
      
      {/* GitHub Icon, Repo and Branch */}
      <div className="flex items-center gap-2 text-gray-600 dark:text-gray-400 min-w-0">
        <Image 
          src="/github-mark.svg" 
          alt="GitHub" 
          width={16} 
          height={16} 
          className="flex-shrink-0 dark:invert"
        />
        <span className="font-medium truncate">{repo}</span>
        <GitBranch className="w-3 h-3 flex-shrink-0 text-gray-500 dark:text-gray-500" />
        <span className="text-sm text-gray-500 dark:text-gray-500 flex-shrink-0">{branch}</span>
      </div>

      {/* Commit Message Input */}
      <div className="flex-1 min-w-0">
        <Input
          value={commitMessage}
          onChange={(e) => setCommitMessage(e.target.value)}
          placeholder="Commit message..."
          className="bg-gray-50 dark:bg-gray-900/50 border-gray-300 dark:border-gray-700/50 text-gray-900 dark:text-gray-100 placeholder-gray-500 dark:placeholder-gray-500 h-8 text-sm"
          disabled={isCommitting || isPushing}
        />
      </div>

      {/* Action Buttons */}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={handleCommit}
          disabled={!commitMessage.trim() || isCommitting || isPushing || isCommitted}
          className="bg-white dark:bg-gray-900/50 border-gray-300 dark:border-gray-700/50 text-gray-900 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 h-8 px-3"
        >
          {isCommitting ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : isCommitted ? (
            <Check className="w-3 h-3" />
          ) : (
            <GitCommit className="w-3 h-3" />
          )}
          <span className="ml-1">{isCommitting ? "Committing..." : isCommitted ? "Committed" : "Commit"}</span>
        </Button>

        <Button
          size="sm"
          onClick={handlePush}
          disabled={!isCommitted || isPushing || isPushed}
          className="bg-blue-600 hover:bg-blue-700 text-white h-8 px-3 shadow-sm"
        >
          {isPushing ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : isPushed ? (
            <Check className="w-3 h-3" />
          ) : (
            <Upload className="w-3 h-3" />
          )}
          <span className="ml-1">{isPushing ? "Pushing..." : isPushed ? "Pushed" : "Push"}</span>
        </Button>
      </div>
    </div>
  )
}

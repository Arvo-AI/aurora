"use client"

import * as React from "react"
import type { JSX } from "react"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { ChevronDown, ChevronUp, X, Check } from "lucide-react"
import CommandLogo from "./CommandLogo"
import { useTheme } from "next-themes"
import { GitHubCommitTool } from "@/components/GitHubCommitTool"
import { useUser } from '@/hooks/useAuthHooks'
import { useChatExpansion } from "@/app/components/ClientShell"

// Import modular utilities
import {
  extractIacPath,
  extractIacAction,
  parseCloudExecCommand,
  parseGitHubToolCommand,
  parseGitHubRcaCommand,
  parseJenkinsRcaCommand,
  parseIacToolCommand,
  parseWebSearchCommand,
  parseAwsMcpCommand,
  parseAwsSuggestCommand,
  parseCorootCommand,
} from "./tool-command-parser"
import { RenderOutput } from "./tool-output-renderer"

interface ToolExecution {
  tool_name: string
  status: "pending" | "running" | "completed" | "error" | "awaiting_confirmation" | "cancelled" | "setting_up_environment"
  input: string
  output?: any
  error?: string | null
  command?: string
  working_directory?: string
  duration?: number
}

interface ToolExecutionWidgetProps {
  tool: ToolCall
  className?: string
  sendMessage?: (query: string, userId: string, additionalData?: any) => boolean
  sendRaw?: (data: string) => boolean
  onToolUpdate?: (updatedTool: Partial<ToolCall>) => void
  sessionId?: string
  userId?: string
}

const ToolExecutionWidget = ({ tool, className, sendMessage, sendRaw, onToolUpdate, sessionId, userId }: ToolExecutionWidgetProps) => {
  const [isTerminalFocused] = React.useState(false)
  const { theme } = useTheme()
  const [editedContent, setEditedContent] = React.useState<string | null>(null)
  const [hasSavedEdit, setHasSavedEdit] = React.useState(false)
  const [lastSavedContent, setLastSavedContent] = React.useState<string | null>(null)
  const { openWorkspace, closeWorkspace, workspaceConfig } = useChatExpansion()

  // SINGLE POINT OF NORMALIZATION: Ensure tool.input is always a string for all downstream parsing
  const normalizedInput = React.useMemo(() => 
    typeof tool.input === 'string' ? tool.input : 
    (tool.input && typeof tool.input === 'object' ? JSON.stringify(tool.input) : ''),
    [tool.input]
  )

  const iacAction = React.useMemo(() => {
    if (tool.tool_name === 'iac_tool') {
      return extractIacAction(normalizedInput, 'write')
    }
    if (tool.tool_name === 'iac_write') return 'write'
    if (tool.tool_name === 'iac_plan') return 'plan'
    if (tool.tool_name === 'iac_apply') return 'apply'
    return undefined
  }, [tool.tool_name, normalizedInput])

  const allowEditing = iacAction === 'write'
  const iacPath = React.useMemo(() => extractIacPath(normalizedInput), [normalizedInput])

  // Use persistent state from tool.isExpanded, default to false if not set
  const showOutput = tool.isExpanded ?? false
  const workspaceActive = React.useMemo(() => {
    if (!sessionId) return false
    return workspaceConfig?.type === 'iac' && workspaceConfig.sessionId === sessionId
  }, [workspaceConfig, sessionId])

  // Auto-expand dropdown when tool starts running or awaiting confirmation
  React.useEffect(() => {
    if ((tool.status === "running" || tool.status === "awaiting_confirmation" || tool.status === "setting_up_environment") && !tool.isExpanded) {
      // Only expand if not already expanded to avoid redundant state updates
      onToolUpdate?.({ isExpanded: true })
    }
  }, [tool.status, tool.isExpanded, onToolUpdate])

  // Handler to toggle output visibility and persist state
  const toggleShowOutput = () => {
    onToolUpdate?.({ isExpanded: !showOutput })
  }

  React.useEffect(() => {
    setEditedContent(null)
    setHasSavedEdit(false)
    setLastSavedContent(null)
  }, [tool.tool_name, tool.output])

  const handleEditorChange = React.useCallback((value: string) => {
    if (!allowEditing) {
      return
    }
    setEditedContent(value)
    setHasSavedEdit(false)
  }, [allowEditing])

  const sendDirectToolCall = React.useCallback(
    (toolName: string, parameters: Record<string, unknown>) => {
      if (!sendRaw || !userId || !sessionId) {
        console.warn(`Cannot invoke ${toolName} without websocket context`)
        return false
      }

      const payload = {
        user_id: userId,
        session_id: sessionId,
        direct_tool_call: {
          tool_name: toolName,
          parameters
        }
      }

      return sendRaw(JSON.stringify(payload))
    },
    [sendRaw, sessionId, userId]
  )

  const handleSave = React.useCallback(
    (content: string) => {
      const success = sendDirectToolCall('iac_tool', { action: 'write', path: iacPath, content })
      if (success) {
        setLastSavedContent(content)
        setHasSavedEdit(true)
        onToolUpdate?.({ status: 'running' })
      }
      return success
    },
    [iacPath, onToolUpdate, sendDirectToolCall]
  )

  const handlePlan = React.useCallback(() => {
    const success = sendDirectToolCall('iac_tool', { action: 'plan' })
    if (success) {
      onToolUpdate?.({ status: 'running' })
    }
    return success
  }, [onToolUpdate, sendDirectToolCall])

  const handleWorkspaceToggle = React.useCallback(() => {
    if (!sessionId || !userId) {
      return
    }

    if (workspaceActive) {
      closeWorkspace()
      return
    }

    openWorkspace({
      type: 'iac',
      sessionId,
      onSave: async (path: string, content: string) => {
        const success = sendDirectToolCall('iac_tool', { action: 'write', path, content })
        if (success) {
          onToolUpdate?.({ status: 'running' })
        }
        return !!success
      },
      onPlan: async () => {
        const success = sendDirectToolCall('iac_tool', { action: 'plan' })
        if (success) {
          onToolUpdate?.({ status: 'running' })
        }
        return !!success
      },
    })
  }, [closeWorkspace, openWorkspace, workspaceActive, onToolUpdate, sendDirectToolCall, sessionId, userId])

  // Parse command for display using modular parsers
  const defaultCliCommand = tool.tool_name ? tool.tool_name.replace(/_/g, " ") : "command"
  let command: string = tool.command || normalizedInput || defaultCliCommand

  // Special display names for specific tools
  if (tool.tool_name === "knowledge_base_search") {
    command = "Knowledge Base"
  }

  // terminal_exec parsing - extract command from input or output
  if (tool.tool_name === "terminal_exec" && !tool.command) {
    try {
      const str = tool.output || normalizedInput
      if (str) {
        const parsed = JSON.parse(str)
        command = parsed.final_command || parsed.kwargs?.command || parsed.command || command
      }
    } catch {
      // Keep default
    }
  }
  // on_prem_kubectl parsing
  else if (tool.tool_name === "on_prem_kubectl" && !tool.command) {
    try {
      const str = tool.output || normalizedInput
      if (str) {
        const parsed = JSON.parse(str)
        command = parsed.command || parsed.kwargs?.command || command
      }
    } catch {
      // Keep default
    }
  }
  // cloud_exec parsing (skip if tool.command already provides a clean command)
  else if (tool.tool_name === "cloud_exec" && !tool.command) {
    const parsed = parseCloudExecCommand(normalizedInput, tool.output, defaultCliCommand)
    command = parsed.command
  }
  // GitHub MCP tools parsing
  else if (tool.tool_name.startsWith("mcp_") && typeof command === "string" && command.trim().startsWith("{")) {
    command = parseGitHubToolCommand(tool.tool_name, command)
  }
  // GitHub RCA tool parsing
  else if (tool.tool_name === "github_rca" && typeof command === "string" && command.trim().startsWith("{")) {
    command = parseGitHubRcaCommand(command)
  }
  // Jenkins RCA tool parsing
  else if (tool.tool_name === "jenkins_rca" && typeof command === "string" && command.trim().startsWith("{")) {
    command = parseJenkinsRcaCommand(normalizedInput)
  }
  // IAC tool parsing
  else if (tool.tool_name === "iac_tool" || tool.tool_name === "iac_write" || tool.tool_name === "iac_plan" || tool.tool_name === "iac_apply") {
    command = parseIacToolCommand(tool.tool_name, normalizedInput, iacAction)
  }
  // Web search parsing
  else if (tool.tool_name === "web_search" && command === defaultCliCommand) {
    command = parseWebSearchCommand(normalizedInput)
  }
  // AWS MCP parsing
  else if (tool.tool_name === "mcp_call_aws" && typeof command === "string" && command.trim().startsWith("{")) {
    command = parseAwsMcpCommand(command)
  }
  else if (tool.tool_name === "mcp_suggest_aws_commands" && typeof command === "string" && command.trim().startsWith("{")) {
    command = parseAwsSuggestCommand(command)
  }
  // Coroot tools parsing
  else if (tool.tool_name.startsWith("coroot_")) {
    command = parseCorootCommand(tool.tool_name, normalizedInput)
  }

  // If command is still JSON blob, use default
  if (typeof command === "string" && command.trim().startsWith("{")) {
    command = defaultCliCommand
  }

  // Final safety check: ensure command is always a string
  if (typeof command !== 'string') {
    command = typeof command === 'object' && command !== null 
      ? JSON.stringify(command, null, 2) 
      : String(command || defaultCliCommand)
  }

  // Extract provider for logo display
  let provider = ''
  try {
    // Try input first
    if (normalizedInput) {
      const parsed = JSON.parse(normalizedInput.replace(/'/g, '"'))
      provider = parsed.provider || parsed.kwargs?.provider || ''
    }
    // Then try output
    if (!provider && tool.output) {
      const outputStr = typeof tool.output === 'string' ? tool.output : JSON.stringify(tool.output)
      const parsed = JSON.parse(outputStr)
      provider = parsed.provider || ''
    }
  } catch {
    // Keep empty provider
  }

  // Special rendering for github_commit tool
  if (tool.tool_name === 'github_commit') {
    let repo = "user/repository"
    let commitMessage = "Update files"
    let branch = "main"
    
    try {
      if (normalizedInput && normalizedInput.includes('{')) {
        const parsed = JSON.parse(normalizedInput)
        repo = parsed.repo || parsed.kwargs?.repo || repo
        commitMessage = parsed.commit_message || parsed.kwargs?.commit_message || commitMessage
        branch = parsed.branch || parsed.kwargs?.branch || branch
      }
    } catch (e) {
      // Use defaults
    }
    
    return (
      <Card className={cn("w-full font-mono text-sm overflow-hidden border border-border", className)} style={{ backgroundColor: theme === 'dark' ? '#000000' : 'white' }}>
        <div className="border-b border-border overflow-hidden" style={{ backgroundColor: theme === 'dark' ? '#000000' : 'white' }}>
          <div className="flex justify-between px-4 py-3">
            <div className="flex items-start gap-2 text-gray-700 dark:text-gray-300 min-w-0 flex-1 overflow-hidden">
              <CommandLogo command={command} toolName={tool.tool_name} provider={provider} />
              <code className="text-sm whitespace-pre-wrap break-all text-gray-700 dark:text-gray-300 flex-1 overflow-wrap-anywhere">{command}</code>
            </div>
          </div>
          <div className="w-full p-4">
            <GitHubCommitTool
              repo={repo}
              branch={branch}
              defaultMessage={commitMessage}
              onCommit={async (message) => {
                if (sendMessage && userId) {
                  const success = sendMessage(
                    `Please use the github_commit tool with these exact parameters: repo="${repo}", commit_message="${message}", branch="${branch}", push=true`,
                    userId,
                    { 
                      tool_suggestion: 'github_commit',
                      session_id: 'current',
                      direct_tool_call: {
                        tool_name: 'github_commit',
                        parameters: {
                          repo: repo,
                          commit_message: message,
                          branch: branch,
                          push: true
                        }
                      }
                    }
                  )
                  if (!success) {
                    throw new Error('Failed to send commit request to backend')
                  }
                } else {
                  throw new Error('Unable to send commit request - no WebSocket connection')
                }
              }}
              onPush={async () => {
                // Push is handled automatically by the commit
              }}
            />
            {tool.output && (
              <div className="mt-2 p-2 bg-green-50 dark:bg-green-900/20 rounded text-sm text-green-700 dark:text-green-300">
                {typeof tool.output === 'string' ? tool.output : JSON.stringify(tool.output)}
              </div>
            )}
            {tool.error && (
              <div className="mt-2 p-2 bg-red-50 dark:bg-red-900/20 rounded text-sm text-red-700 dark:text-red-300">
                {tool.error}
              </div>
            )}
          </div>
        </div>
      </Card>
    )
  }

  return (
    <Card className={cn("w-full font-mono text-sm overflow-hidden border border-border", className)} style={{ backgroundColor: theme === 'dark' ? '#000000' : 'white' }}>
      {/* Terminal Header */}
      <div className="border-b border-border overflow-hidden" style={{ backgroundColor: theme === 'dark' ? '#000000' : 'white' }}>
        <div className="flex justify-between px-4 py-3">
          <div className="flex items-start gap-2 text-gray-700 dark:text-gray-300 min-w-0 flex-1 overflow-hidden">
            <CommandLogo command={command} toolName={tool.tool_name} provider={provider} />
            <code className="text-sm whitespace-pre-wrap break-all text-gray-700 dark:text-gray-300 flex-1 overflow-wrap-anywhere">{command}</code>
            {tool.tool_name === "cloud_exec" && (() => {
              try {
                const d = JSON.parse(tool.output as any)
                const displayName = (d as any)?.resource_name || (d as any)?.resource_id
                return displayName ? (
                  <span className="text-xs text-muted-foreground ml-2">
                    ({displayName})
                  </span>
                ) : null
              } catch {
                return null
              }
            })()}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0 ml-2">
            {allowEditing && userId && sessionId && (
              <Button
                size="sm"
                variant="ghost"
                className="h-6 px-2 text-xs font-medium text-foreground hover:bg-muted/50"
                onClick={handleWorkspaceToggle}
              >
                {workspaceActive ? "Close workspace" : "Open workspace"}
              </Button>
            )}
            <button
              onClick={toggleShowOutput}
              aria-label={showOutput ? "Collapse output" : "Expand output"}
              className="flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors"
            >
              {showOutput ? (
                <ChevronUp className="h-4 w-4" />
              ) : (
                <ChevronDown className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>

        {/* Terminal Output */}
        {showOutput && (isTerminalFocused || tool.status === "running" || tool.status === "setting_up_environment" || tool.status === "awaiting_confirmation" || tool.output || tool.error) && (
          <div className="border-t border-border max-h-96 overflow-y-auto" style={{ backgroundColor: theme === 'dark' ? '#000000' : 'white' }}>

            {/* Show "Setting up environment" when terminal pod is being created */}
            {tool.status === "setting_up_environment" && (
              <div className="px-4 py-3 flex items-center gap-3 text-muted-foreground">
                <div className="h-4 w-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                <span className="text-sm">Setting up environment...</span>
              </div>
            )}

            {/* Show message when awaiting confirmation */}
            {tool.status === "awaiting_confirmation" && !tool.output && !tool.error && (
              <div className="border-t border-border bg-muted/30 px-4 py-3 flex items-center justify-between gap-3">
                <span className="text-sm text-muted-foreground">
                  {(tool as any).confirmation_message || "This action requires confirmation"}
                </span>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 px-2 text-xs font-medium hover:bg-background"
                    onClick={() => {
                      const confirmationId = (tool as any).confirmation_id
                      if (!confirmationId || !sendRaw || !userId) return
                      
                      sendRaw(JSON.stringify({
                        type: 'confirmation_response',
                        confirmation_id: confirmationId,
                        decision: 'cancel',
                        user_id: userId,
                        session_id: sessionId,
                      }))
                      
                      onToolUpdate?.({ status: 'completed', output: 'Operation cancelled by user' })
                    }}
                  >
                    <X className="h-3 w-3 mr-1" />
                    Decline
                  </Button>
                  <Button
                    size="sm"
                    className="h-6 px-2 text-xs font-medium bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    onClick={() => {
                      const confirmationId = (tool as any).confirmation_id
                      if (!confirmationId || !sendRaw || !userId) return
                      
                      sendRaw(JSON.stringify({
                        type: 'confirmation_response',
                        confirmation_id: confirmationId,
                        decision: 'execute',
                        user_id: userId,
                        session_id: sessionId,
                      }))
                      
                      onToolUpdate?.({ status: 'running' })
                    }}
                  >
                    <Check className="h-3 w-3 mr-1" />
                    Confirm
                  </Button>
                </div>
              </div>
            )}

            {/* Show shimmer effect while tool is running and no output yet */}
            {tool.status === "running" && !tool.output && !tool.error && tool.status !== "setting_up_environment" && (
              <div className="px-4 py-3 space-y-2">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-4 w-5/6" />
                <Skeleton className="h-4 w-2/3" />
              </div>
            )}

            {tool.output && (
              <div className="px-4 py-3" style={{ backgroundColor: theme === 'dark' ? '#000000' : 'white' }}>
                <RenderOutput
                  output={tool.output}
                  toolName={tool.tool_name}
                  theme={theme || 'dark'}
                  allowEditing={allowEditing}
                  editedContent={editedContent}
                  lastSavedContent={lastSavedContent}
                  handleEditorChange={handleEditorChange}
                  handleSave={handleSave}
                  handlePlan={handlePlan}
                  hasSavedEdit={hasSavedEdit}
                  sendRaw={sendRaw}
                  userId={userId}
                  sessionId={sessionId}
                />
              </div>
            )}

            {tool.error && (
              <div className="px-4 py-3">
                <div className="text-red-600 dark:text-red-400 text-xs mb-2">Error:</div>
                <pre className="text-red-600 dark:text-red-300 text-xs leading-relaxed whitespace-pre-wrap">{tool.error}</pre>
              </div>
            )}

            {isTerminalFocused && tool.status !== "running" && (
              <div className="px-4 py-2 border-t border-gray-200 dark:border-gray-700">
                <div className="flex items-center gap-1 text-gray-500 dark:text-gray-400">
                  <CommandLogo command={command} toolName={tool.tool_name} provider={provider} />
                  <div className="w-2 h-4 bg-gray-500 dark:bg-gray-400 animate-pulse ml-1"></div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  )
}

export default ToolExecutionWidget

/**
 * Output rendering utilities for different tool types.
 * Handles syntax highlighting, formatting, and special displays.
 * Extensible for future Pulumi support.
 */

import * as React from "react"
import Convert from 'ansi-to-html'
import { Button } from "@/components/ui/button"
import { IaCEditorPanel } from "./iac-editor-panel"

interface RenderOutputProps {
  output: any
  toolName: string
  theme: string
  allowEditing: boolean
  editedContent: string | null
  lastSavedContent: string | null
  handleEditorChange: (value: string) => void
  handleSave: (content: string) => void
  handlePlan: () => void
  hasSavedEdit: boolean
  sendRaw?: (data: string) => boolean
  userId?: string
  sessionId?: string
}

// Helper to detect tool type for appropriate rendering
const isCliTool = (toolName: string) => {
  return toolName.includes('cloud_exec') || toolName.includes('kubectl') || 
         toolName.includes('gcloud') || toolName.includes('aws') || toolName.includes('azure')
}

const isIacTool = (toolName: string) => {
  return toolName.includes('iac') || toolName.includes('terraform')
}

const isWebSearchTool = (toolName: string) => {
  return toolName.includes('web_search')
}

const isHclContent = (content: string) => {
  const trimmed = content.trim()
  return trimmed.includes('resource "') || trimmed.includes('provider "') || 
         trimmed.includes('variable "') || trimmed.includes('output "')
}

export function RenderOutput({
  output,
  toolName,
  theme,
  allowEditing,
  editedContent,
  lastSavedContent,
  handleEditorChange,
  handleSave,
  handlePlan,
  hasSavedEdit,
  sendRaw,
  userId,
  sessionId
}: RenderOutputProps): JSX.Element | null {
  // Initialize ANSI to HTML converter
  const ansiConverter = React.useMemo(() => new Convert({
    fg: theme === 'dark' ? '#e5e5e5' : '#333333',
    bg: 'transparent',
    newline: true,
    escapeXML: true
  }), [theme])

  if (typeof output !== "string") {
    try {
      return (
        <pre className="text-gray-700 dark:text-gray-300 text-xs leading-relaxed whitespace-pre-wrap">
          {JSON.stringify(output, null, 2)}
        </pre>
      )
    } catch (e) {
      return (
        <div className="text-red-600 dark:text-red-400 text-xs">Error rendering output: {String(e)}</div>
      )
    }
  }

  if (!output) return null

  // Try to parse JSON first
  let parsed: any = null
  const trimmed = output.trim()
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      parsed = JSON.parse(trimmed)
    } catch {
      /* fallthrough to raw */
    }
  }

  // If not JSON, check tool type for appropriate rendering
  if (!parsed) {
    // IAC tools with HCL content - use Monaco Editor
    if (isIacTool(toolName) && isHclContent(output)) {
      const trimmedOutput = output.trim()
      const lineCount = trimmedOutput.split('\n').length
      const height = Math.min(Math.max(lineCount * 18 + 20, 100), 300)

      const baselineContent = lastSavedContent ?? trimmedOutput
      const currentValue = allowEditing ? (editedContent ?? trimmedOutput) : trimmedOutput
      const isDirty = allowEditing && currentValue !== baselineContent

      const viewer = (
        <IaCEditorPanel
          value={currentValue}
          onChange={handleEditorChange}
          readOnly={!allowEditing}
          height={height}
          themeMode={theme}
          language="terraform"
        />
      )

      if (!allowEditing) {
        return viewer
      }

      return (
        <div className="space-y-0.2">
          {viewer}
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="secondary"
              className="h-6 px-2 text-xs font-medium"
              disabled={
                !isDirty ||
                !currentValue.trim() ||
                !sendRaw ||
                !userId ||
                !sessionId
              }
              onClick={() => handleSave(currentValue)}
            >
              Save to Terraform
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-xs font-medium"
              disabled={
                !hasSavedEdit ||
                isDirty ||
                !sendRaw ||
                !userId ||
                !sessionId
              }
              onClick={handlePlan}
            >
              Re-run Plan
            </Button>
          </div>
        </div>
      )
    }

    // CLI tools - use ANSI to HTML for terminal colors
    if (isCliTool(toolName)) {
      const htmlOutput = ansiConverter.toHtml(output)
      return (
        <div 
          className="text-xs leading-relaxed whitespace-pre-wrap font-mono"
          dangerouslySetInnerHTML={{ __html: htmlOutput }}
        />
      )
    }

    // Highlight plain-text Terraform apply failures in red
    if (isIacTool(toolName) && /terraform apply failed/i.test(output)) {
      return (
        <pre className="text-red-600 dark:text-red-300 text-xs leading-relaxed whitespace-pre-wrap">{output}</pre>
      )
    }

    // Terraform text beautifier (non-JSON, non-ANSI/hcl)
    if (isIacTool(toolName)) {
      const isNoChanges = /\bno changes\b/i.test(output)
      const isApplied = /applied successfully/i.test(output)
      const isPlanned = /\bplan\b/i.test(output) && !isApplied

      // Split into paragraphs by blank lines; fall back to single paragraph
      const paragraphs = output
        .trim()
        .split(/\n\s*\n+/)
        .map(p => p.trim())
        .filter(Boolean)

      const first = paragraphs[0] || output.trim()
      const rest = paragraphs.slice(1)

      const firstColor = isApplied || isNoChanges
        ? 'text-green-600 dark:text-green-400'
        : isPlanned
          ? 'text-blue-600 dark:text-blue-400'
          : 'text-foreground'

      return (
        <div className="space-y-2 text-sm">
          <div className={`font-medium ${firstColor}`}>{first}</div>
          {rest.length > 0 && (
            <div className="space-y-2 text-gray-700 dark:text-gray-300">
              {rest.map((p, idx) => {
                // If a paragraph looks like a list, render as list
                const lines = p.split(/\n+/)
                const isList = lines.every(l => /^[-*]\s+/.test(l) || l.trim() === '')
                if (isList) {
                  return (
                    <ul key={idx} className="list-disc pl-5">
                      {lines.filter(l => l.trim()).map((l, i) => (
                        <li key={i} className="whitespace-pre-wrap">{l.replace(/^[-*]\s+/, '')}</li>
                      ))}
                    </ul>
                  )
                }
                return (
                  <p key={idx} className="whitespace-pre-wrap leading-relaxed">{p}</p>
                )
              })}
            </div>
          )}
        </div>
      )
    }

    // Web search results formatting
    if (isWebSearchTool(toolName)) {
      return (
        <div className="space-y-2 text-sm">
          <div className="text-blue-600 dark:text-blue-400 font-medium whitespace-pre-wrap">{output}</div>
        </div>
      )
    }

    // Fallback: plain text
    return (
      <pre className="text-gray-700 dark:text-gray-300 text-xs leading-relaxed whitespace-pre-wrap">{output}</pre>
    )
  }

  // Handle response too large case - show as normal message, not error
  if (parsed && typeof parsed === 'object' && (parsed as any).response_too_large) {
    const message = (parsed as any).message || "Response is too large, retrying with filtered command"
    return (
      <div className="space-y-2">
        <div className="text-gray-500 dark:text-gray-500 text-sm whitespace-pre-wrap">{message}</div>
      </div>
    )
  }

  // Handle error structure
  if (parsed.error) {
    return (
      <div className="space-y-2">
        <div className="text-red-600 dark:text-red-400 text-xs font-medium">Error</div>
        <div className="text-sm text-red-300 whitespace-pre-wrap">{parsed.error}</div>
      </div>
    )
  }

  // If Terraform status indicates failure, render a clear red error block
  const isTerraformFailure =
    (parsed && typeof parsed === 'object' && (
      (parsed as any).status === 'failed' ||
      (parsed as any).summary?.apply === 'failed'
    )) || false

  if (isTerraformFailure) {
    const applyStep = Array.isArray((parsed as any).results)
      ? (parsed as any).results.find((r: any) => r.step === 'terraform_apply')
      : null
    const stderr = applyStep?.result?.stderr
    const stdout = applyStep?.result?.stdout
    const headline = (parsed as any).chat_output || (parsed as any).message || 'Terraform apply failed'
    const body = stderr || stdout || (parsed as any).error || ''
    const suggestion = (parsed as any).error_analysis?.suggested_fix

    return (
      <div className="space-y-2 px-0">
        <div className="text-red-600 dark:text-red-400 text-xs font-semibold">{headline}</div>
        {suggestion && (
          <div className="text-red-500/90 dark:text-red-300/90 text-xs">Suggested fix: {suggestion}</div>
        )}
        {body && (
          <pre className="text-red-600 dark:text-red-300 text-xs leading-relaxed whitespace-pre-wrap">{body}</pre>
        )}
      </div>
    )
  }

  // Handle resources list at root level or nested under .data
  const resourcesArray = Array.isArray(parsed.resources)
    ? parsed.resources
    : Array.isArray((parsed as any).data?.resources)
      ? (parsed as any).data.resources
      : Array.isArray((parsed as any).data?.items)
        ? (parsed as any).data.items
        : null

  if (resourcesArray) {
    return (
      <div className="space-y-3">
        <div className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider font-medium">
          Resources ({(parsed.total_count ?? (parsed as any).data?.total_count ?? resourcesArray.length)})
        </div>
        <div className="space-y-2">
          {resourcesArray.map((res: any, idx: number) => (
            <div
              key={idx}
              className="border border-gray-200 dark:border-gray-700 rounded-md p-3 bg-white/5 dark:bg-gray-800/40"
            >
              <div className="flex items-center justify-between text-sm font-semibold mb-1">
                <span>{res.name || res.id || `Resource ${idx + 1}`}</span>
                {res.status && (
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {typeof res.status === 'object' ? JSON.stringify(res.status) : res.status}
                  </span>
                )}
              </div>
              <div className="text-xs text-gray-600 dark:text-gray-300 grid grid-cols-2 gap-x-4 gap-y-1">
                {Object.entries(res)
                  .filter(([key]) => key !== "name" && key !== "status")
                  .map(([key, value]) => (
                    <React.Fragment key={key}>
                      <span className="font-medium capitalize col-span-1">{key}</span>
                      <span className="col-span-1 break-all">{String(value)}</span>
                    </React.Fragment>
                  ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  // Handle web search results specially
  if (isWebSearchTool(toolName) && parsed && typeof parsed === 'object') {
    const results = (parsed as any).results || []
    const status = (parsed as any).status
    
    if (status === 'cancelled') {
      return (
        <div className="text-yellow-600 dark:text-yellow-400 text-sm">
          Search cancelled by user
        </div>
      )
    }
    
    if (status === 'skipped') {
      return (
        <div className="text-gray-600 dark:text-gray-400 text-sm">
          {(parsed as any).message || 'Search skipped'}
        </div>
      )
    }
    
    if (Array.isArray(results) && results.length > 0) {
      return (
        <div className="space-y-3">
          <div className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider font-medium">
            Search Results ({results.length})
          </div>
          <div className="space-y-3">
            {results.map((result: any, idx: number) => (
              <div
                key={idx}
                className="border border-gray-200 dark:border-gray-700 rounded-md p-3 bg-white/5 dark:bg-gray-800/40"
              >
                <div className="flex items-start justify-between mb-2">
                  <a 
                    href={result.url} 
                    target="_blank" 
                    rel="noopener noreferrer"
                    className="text-blue-600 dark:text-blue-400 font-medium text-sm hover:underline"
                  >
                    {result.title || result.url}
                  </a>
                  <span className="text-xs text-gray-500 dark:text-gray-400 ml-2">
                    {result.domain}
                  </span>
                </div>
                {result.summary && (
                  <div className="text-xs text-gray-600 dark:text-gray-300 mb-2">
                    {result.summary}
                  </div>
                )}
                {result.key_points && Array.isArray(result.key_points) && result.key_points.length > 0 && (
                  <ul className="text-xs text-gray-600 dark:text-gray-300 list-disc pl-4 space-y-1">
                    {result.key_points.slice(0, 3).map((point: string, pointIdx: number) => (
                      <li key={pointIdx}>{point}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )
    }
  }

  // After successfully parsing JSON, check for chat_output field first
  if (parsed && typeof parsed === 'object' && 'chat_output' in parsed) {
    const chatOutput = String((parsed as any).chat_output)

    // Apply the same smart rendering logic to chat_output
    if (isIacTool(toolName) && isHclContent(chatOutput)) {
      const trimmedChatOutput = chatOutput.trim()
      const lineCount = trimmedChatOutput.split('\n').length
      const height = Math.min(Math.max(lineCount * 18 + 20, 100), 300)

      const baselineContent = lastSavedContent ?? trimmedChatOutput
      const currentValue = allowEditing ? (editedContent ?? trimmedChatOutput) : trimmedChatOutput
      const isDirty = allowEditing && currentValue !== baselineContent

      const viewer = (
        <IaCEditorPanel
          value={currentValue}
          onChange={allowEditing ? handleEditorChange : () => {}}
          readOnly={!allowEditing}
          height={height}
          themeMode={theme}
          language="terraform"
        />
      )

      if (!allowEditing) {
        return viewer
      }

      return (
        <div className="space-y-0.2">
          {viewer}
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="secondary"
              className="h-6 px-2 text-xs font-medium"
              disabled={
                !isDirty ||
                !currentValue.trim() ||
                !sendRaw ||
                !userId ||
                !sessionId
              }
              onClick={() => handleSave(currentValue)}
            >
              Save to Terraform
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-xs font-medium"
              disabled={
                !hasSavedEdit ||
                isDirty ||
                !sendRaw ||
                !userId ||
                !sessionId
              }
              onClick={handlePlan}
            >
              Re-run Plan
            </Button>
          </div>
        </div>
      )
    }

    if (isCliTool(toolName)) {
      const htmlOutput = ansiConverter.toHtml(chatOutput)
      return (
        <div 
          className="text-xs leading-relaxed whitespace-pre-wrap font-mono"
          dangerouslySetInnerHTML={{ __html: htmlOutput }}
        />
      )
    }

    return (
      <div className="text-foreground text-sm whitespace-pre-wrap">
        {chatOutput}
      </div>
    )
  }

  // Fallback: pretty json
  return (
    <pre className="text-gray-700 dark:text-gray-300 text-xs leading-relaxed whitespace-pre-wrap">
      {JSON.stringify(parsed, null, 2)}
    </pre>
  )
}

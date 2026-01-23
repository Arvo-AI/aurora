"use client"

import * as React from "react"

import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { useToast } from "@/components/ui/use-toast"
import { configureMonaco } from "@/lib/monacoTerraform"
import { cn } from "@/lib/utils"
import { AlertTriangle, ChevronsLeft, Copy, Download, File, Folder } from "lucide-react"

const MonacoEditor = React.lazy(() => import("@monaco-editor/react"))

export type IaCFileNode = {
  name: string
  path: string
  type: "file" | "directory"
  size?: number
  updated_at?: string
  children?: IaCFileNode[]
}

interface IaCWorkspaceProps {
  sessionId: string
  onClose?: () => void
  onSave?: (path: string, content: string) => Promise<boolean>
  onPlan?: () => Promise<boolean>
}

export function IaCWorkspace({ sessionId, onClose, onSave, onPlan }: IaCWorkspaceProps) {
  const [isLoading, setIsLoading] = React.useState(true)
  const [tree, setTree] = React.useState<IaCFileNode[]>([])
  const [selectedFile, setSelectedFile] = React.useState<IaCFileNode | null>(null)
  const [fileContent, setFileContent] = React.useState<string>("")
  const [originalFileContent, setOriginalFileContent] = React.useState<string>("") // Store original content for revert
  const [isSaving, setIsSaving] = React.useState(false)
  const [isPlanning, setIsPlanning] = React.useState(false)
  const [hasUnsavedChanges, setHasUnsavedChanges] = React.useState(false)
  const [sidebarWidthPercent, setSidebarWidthPercent] = React.useState(20)
  const resizeStateRef = React.useRef<{ startX: number; startWidthPercent: number; containerWidth: number } | null>(null)
  const [isResizing, setIsResizing] = React.useState(false)
  const containerRef = React.useRef<HTMLDivElement>(null)
  const { toast } = useToast()

  const loadTree = React.useCallback(async () => {
    setIsLoading(true)
    try {
      const response = await fetch(`/api/terraform/workspace/files?session_id=${encodeURIComponent(sessionId)}`)
      if (!response.ok) {
        throw new Error(`Failed to load files (${response.status})`)
      }
      const data = await response.json()
      const files = data.files ?? []
      setTree(files)
      
      // Auto-open the first file if available
      const findFirstFile = (nodes: IaCFileNode[]): IaCFileNode | null => {
        for (const node of nodes) {
          if (node.type === "file") {
            return node
          }
          if (node.children) {
            const found = findFirstFile(node.children)
            if (found) return found
          }
        }
        return null
      }
      
      const firstFile = findFirstFile(files)
      if (firstFile) {
        // Load the first file automatically
        try {
          const fileResponse = await fetch(`/api/terraform/workspace/file?session_id=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(firstFile.path)}`)
          if (fileResponse.ok) {
            const fileData = await fileResponse.json()
            const content = (fileData.content ?? "").trim()
            setFileContent(content)
            setOriginalFileContent(content) // Store original for revert
            setSelectedFile(firstFile)
            setHasUnsavedChanges(false)
          }
        } catch (error) {
          // Silently fail if auto-loading the first file fails
          console.warn('Failed to auto-load first file:', error)
        }
      }
    } catch (error) {
      toast({
        title: "Failed to load workspace",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive"
      })
    } finally {
      setIsLoading(false)
    }
  }, [sessionId, toast])

  const loadFile = React.useCallback(async (node: IaCFileNode) => {
    if (node.type !== "file") return
    try {
      const response = await fetch(`/api/terraform/workspace/file?session_id=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(node.path)}`)
      if (!response.ok) {
        throw new Error(`Failed to read ${node.name}`)
      }
      const data = await response.json()
      const content = (data.content ?? "").trim()
      setFileContent(content)
      setOriginalFileContent(content) // Store original for revert
      setSelectedFile(node)
      setHasUnsavedChanges(false)
    } catch (error) {
      toast({
        title: "Unable to open file",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive"
      })
    }
  }, [sessionId, toast])

  React.useEffect(() => {
    loadTree()
  }, [loadTree])

  const handleSave = React.useCallback(async () => {
    if (!selectedFile || !onSave) return
    setIsSaving(true)
    try {
      const success = await onSave(selectedFile.path, fileContent)
      if (success) {
        toast({ title: "Terraform file saved", description: selectedFile.name })
        setOriginalFileContent(fileContent) // Update original after successful save
        setHasUnsavedChanges(false)
        await loadTree()
      } else {
        throw new Error("Save rejected")
      }
    } catch (error) {
      toast({
        title: "Save failed",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive"
      })
    } finally {
      setIsSaving(false)
    }
  }, [fileContent, loadTree, onSave, selectedFile, toast])

  const handleRevert = React.useCallback(() => {
    setFileContent(originalFileContent)
    setHasUnsavedChanges(false)
    toast({ title: "Changes reverted" })
  }, [originalFileContent, toast])

  const handlePlan = React.useCallback(async () => {
    if (!onPlan) return
    setIsPlanning(true)
    try {
      const success = await onPlan()
      if (success) {
        toast({ title: "Terraform plan queued" })
      } else {
        throw new Error("Plan rejected")
      }
    } catch (error) {
      toast({
        title: "Plan failed",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive"
      })
    } finally {
      setIsPlanning(false)
    }
  }, [onPlan, toast])

  const handleCopy = React.useCallback(() => {
    if (!fileContent) return
    navigator.clipboard.writeText(fileContent)
    toast({ title: "Copied to clipboard" })
  }, [fileContent, toast])

  const handleDownload = React.useCallback(() => {
    if (!selectedFile || !fileContent) return
    const blob = new Blob([fileContent], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = selectedFile.name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    toast({ title: "File downloaded" })
  }, [selectedFile, fileContent, toast])

  const handleSidebarPointerDown = React.useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    const containerWidth = containerRef.current?.offsetWidth || 800
    resizeStateRef.current = {
      startX: event.clientX,
      startWidthPercent: sidebarWidthPercent,
      containerWidth: containerWidth,
    }
    setIsResizing(true)
  }, [sidebarWidthPercent])

  React.useEffect(() => {
    if (!isResizing) {
      return
    }

    const handlePointerMove = (event: PointerEvent) => {
      if (!resizeStateRef.current) return
      const { startX, startWidthPercent, containerWidth } = resizeStateRef.current
      const delta = event.clientX - startX
      const deltaPercent = (delta / containerWidth) * 100
      const nextPercent = Math.min(50, Math.max(15, startWidthPercent + deltaPercent))
      setSidebarWidthPercent(nextPercent)
    }

    const stopResizing = () => {
      setIsResizing(false)
      resizeStateRef.current = null
    }

    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopResizing, { once: true })

    return () => {
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResizing)
    }
  }, [isResizing])

  const renderTree = (nodes: IaCFileNode[]) => (
    <ul className="space-y-1">
      {nodes.map((node) => (
        <li key={node.path}>
          <button
            className={cn(
              "w-full text-left text-sm p-2 rounded hover:bg-muted transition flex items-center gap-2",
              selectedFile?.path === node.path && "bg-muted"
            )}
            onClick={() => (node.type === "file" ? loadFile(node) : undefined)}
          >
            {node.type === "directory" ? (
              <Folder className="h-4 w-4 flex-shrink-0" />
            ) : (
              <File className="h-4 w-4 flex-shrink-0" />
            )}
            <span>{node.name}</span>
          </button>
          {node.children && node.children.length > 0 && (
            <div className="pl-4 border-l border-border mt-1">
              {renderTree(node.children)}
            </div>
          )}
        </li>
      ))}
    </ul>
  )

  return (
    <Card ref={containerRef} className="flex flex-col h-full w-full p-4">
      <div className="flex items-center mb-1 -mt-2">
        <Button 
          variant="ghost" 
          size="sm"
          onClick={onClose}
          className="h-6 w-6 p-0 hover:bg-muted"
        >
          <ChevronsLeft className="h-3.5 w-3.5" />
        </Button>
      </div>
      
      <div className="flex flex-1 min-h-0">
      <div
        className="flex-shrink-0 overflow-hidden border-r border-border"
        style={{ width: `${sidebarWidthPercent}%` }}
      >
        <div className="border-b p-3 font-semibold text-sm">Files</div>
        <ScrollArea className="h-full">
          <div className="p-2">
            {isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-6" />
                <Skeleton className="h-6" />
                <Skeleton className="h-6" />
              </div>
            ) : tree.length === 0 ? (
              <p className="text-sm text-muted-foreground">No Terraform files yet.</p>
            ) : (
              renderTree(tree)
            )}
          </div>
        </ScrollArea>
      </div>

      <div
        className="hidden sm:flex w-3 cursor-col-resize flex-shrink-0 items-center justify-start group"
        onPointerDown={handleSidebarPointerDown}
        title="Drag left to expand code editor️"
      >
        <div className="w-0.5 h-full opacity-0 group-hover:opacity-100 bg-muted-foreground/50 transition-opacity" />
      </div>

      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center justify-between mb-2">
          <p className="text-sm font-semibold text-muted-foreground">
              {selectedFile 
                ? selectedFile.path.split('/').join(' > ')
                : "Select a Terraform file"}
            </p>
          <div className="flex gap-1">
            <Button 
              variant="ghost" 
              size="sm"
              onClick={handleCopy}
              disabled={!selectedFile || !fileContent}
              className="h-8 w-8 p-0 hover:bg-muted"
            >
              <Copy className="h-4 w-4" />
            </Button>
            <Button 
              variant="ghost" 
              size="sm"
              onClick={handleDownload}
              disabled={!selectedFile || !fileContent}
              className="h-8 w-8 p-0 hover:bg-muted"
            >
              <Download className="h-4 w-4" />
            </Button>
          </div>
        </div>

        <div className="flex-1 overflow-hidden relative" style={{ backgroundColor: '#000000' }}>
          <div className="h-full" style={{ backgroundColor: '#000000' }}>
            {selectedFile ? (
              <React.Suspense fallback={<Skeleton className="h-full w-full" />}>
                <MonacoEditor
                  value={fileContent}
                  language="terraform"
                  theme="terraform-dark"
                  options={{ 
                    minimap: { enabled: false }, 
                    fontSize: 13, 
                    readOnly: false, 
                    automaticLayout: true,
                  }}
                  beforeMount={configureMonaco}
                  onChange={(value) => {
                    setFileContent(value ?? "")
                    setHasUnsavedChanges(true)
                  }}
                />
              </React.Suspense>
            ) : (
              <div className="p-6 text-sm text-muted-foreground">Choose a file from the tree to start editing.</div>
            )}
          </div>
          
          {/* Floating Save/Revert Bar */}
          {hasUnsavedChanges && selectedFile && (
            <div className="absolute bottom-6 left-1/2 -translate-x-1/2 flex items-center gap-3 px-5 py-2.5 rounded-full bg-black/90 backdrop-blur-sm border border-white/10 shadow-xl">
              <div className="flex items-center gap-2 text-sm text-gray-300">
                <AlertTriangle className="h-4 w-4" />
                <span>Unsaved Changes</span>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={handleRevert}
                disabled={isSaving}
                className="h-8 px-4 rounded-full bg-transparent hover:bg-white/10 border-white/20"
              >
                Reset
              </Button>
              <Button
                size="sm"
                onClick={handleSave}
                disabled={isSaving}
                className="h-8 px-4 rounded-full bg-blue-600 hover:bg-blue-700"
              >
                {isSaving ? "Saving..." : "Save"}
              </Button>
            </div>
          )}
        </div>
      </div>
      </div>
    </Card>
  )
}

export default IaCWorkspace

/**
 * Code editor panel for IaC content (Terraform HCL).
 * Uses Monaco Editor with syntax highlighting.
 * Reusable for future Pulumi support (TypeScript/Python/YAML).
 */

import * as React from "react"
import type { JSX } from "react"
import dynamic from 'next/dynamic'
import { configureMonaco } from "@/lib/monacoTerraform"

// Lazy load Monaco Editor - only loads when IAC tools need syntax highlighting
const Editor = dynamic(() => import('@monaco-editor/react'), {
  ssr: false,
  loading: () => <div className="bg-muted animate-pulse rounded-md h-full" />,
})

type MonacoEditorLike = {
  getDomNode: () => HTMLElement | null
}

type MonacoLike = {
  editor: {
    defineTheme: (themeName: string, themeData: unknown) => void
    setTheme: (themeName: string) => void
  }
}

interface IaCEditorPanelProps {
  value: string
  onChange: (value: string) => void
  readOnly?: boolean
  height: number
  themeMode: string
  language?: string
}

const applyMonacoTheming = (
  editor: MonacoEditorLike,
  monaco: MonacoLike,
  themeMode: string
) => {
  // Register terraform language and syntax highlighting
  configureMonaco(monaco)
  
  if (themeMode === 'dark') {
    monaco.editor.defineTheme('custom-dark', {
      base: 'vs-dark',
      inherit: true,
      rules: [
        { token: '', foreground: 'ffffff', background: '000000' },
        { token: 'string', foreground: 'ce9178' },
        { token: 'keyword', foreground: '569cd6' },
        { token: 'number', foreground: 'b5cea8' },
        { token: 'comment', foreground: '6a9955' }
      ],
      colors: {
        'editor.background': '#000000',
        'editor.foreground': '#ffffff',
        'editor.lineHighlightBackground': '#0a0a0a',
        'editorLineNumber.foreground': '#666666',
        'editorLineNumber.activeForeground': '#ffffff',
        'editorIndentGuide.background': '#404040',
        'editorIndentGuide.activeBackground': '#707070'
      }
    })
    monaco.editor.setTheme('custom-dark')
  }

  const domNode = editor.getDomNode()
  if (!domNode) {
    return
  }

  domNode.style.padding = '0px'
  domNode.style.margin = '0px'

  const selectors = [
    '.monaco-editor',
    '.overflow-guard',
    '.monaco-scrollable-element',
    '.view-lines',
    '.view-line',
    '.content-widgets',
    '.overlays',
    '.margin',
    '.glyph-margin',
    '.lines-content'
  ]

  selectors.forEach((selector) => {
    const elements = domNode.querySelectorAll(selector)
    elements.forEach((el: Element) => {
      const element = el as HTMLElement
      element.style.padding = '0px'
      element.style.margin = '0px'
    })
  })
}

export const IaCEditorPanel = ({
  value,
  onChange,
  readOnly = false,
  height,
  themeMode,
  language = 'terraform'
}: IaCEditorPanelProps): JSX.Element => (
  <div
    className="overflow-hidden"
    style={{ height: `${height}px`, backgroundColor: themeMode === 'dark' ? '#000000' : '#ffffff', padding: '0px' }}
  >
    <Editor
      height={`${height}px`}
      language={language}
      theme={themeMode === 'dark' ? 'custom-dark' : 'vs-light'}
      value={value}
      onChange={(newValue: string | undefined) => onChange(newValue ?? '')}
      options={{
        readOnly,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 12,
        lineNumbers: 'on',
        lineNumbersMinChars: 3,
        wordWrap: 'on',
        automaticLayout: true,
        padding: { top: 0, bottom: 0 },
        scrollbar: {
          vertical: 'auto',
          horizontal: 'auto'
        }
      }}
      onMount={(editor: MonacoEditorLike, monacoInstance: MonacoLike) => {
        applyMonacoTheming(editor, monacoInstance, themeMode)
      }}
    />
  </div>
)

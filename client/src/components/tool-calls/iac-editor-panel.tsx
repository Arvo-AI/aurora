/**
 * Code editor panel for IaC content (Terraform HCL).
 * Uses CodeMirror 6 with syntax highlighting.
 */

import * as React from "react"
import { useRef, useEffect, useCallback } from "react"
import type { JSX } from "react"
import { EditorState } from "@codemirror/state"
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightSpecialChars } from "@codemirror/view"
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands"
import { syntaxHighlighting, defaultHighlightStyle, StreamLanguage } from "@codemirror/language"
import { oneDark } from "@codemirror/theme-one-dark"

const hclLanguage = StreamLanguage.define({
  token(stream) {
    if (stream.match(/\/\/.*/)) return "comment"
    if (stream.match(/\/\*/)) {
      while (!stream.match(/\*\//)) {
        if (!stream.next()) break
      }
      return "comment"
    }
    if (stream.match(/#.*/)) return "comment"
    if (stream.match(/"(?:[^"\\]|\\.)*"/)) return "string"
    if (stream.match(/<<-?\w+/)) return "string"
    if (stream.match(/\b(resource|provider|variable|output|data|module|terraform|locals|backend|required_providers|required_version)\b/)) return "keyword"
    if (stream.match(/\b(string|number|bool|list|map|set|object|tuple|any)\b/)) return "typeName"
    if (stream.match(/\b(true|false|null)\b/)) return "atom"
    if (stream.match(/\b\d+(\.\d+)?\b/)) return "number"
    if (stream.match(/[{}\[\]()=]/)) return "punctuation"
    if (stream.match(/[a-zA-Z_][\w-]*/)) return "variableName"
    stream.next()
    return null
  },
  startState() { return {} },
})

interface IaCEditorPanelProps {
  value: string
  onChange: (value: string) => void
  readOnly?: boolean
  height: number
  themeMode: string
  language?: string
}

export const IaCEditorPanel = ({
  value,
  onChange,
  readOnly = false,
  height,
  themeMode,
}: IaCEditorPanelProps): JSX.Element => {
  const containerRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  const createExtensions = useCallback(() => {
    const extensions = [
      lineNumbers(),
      highlightActiveLine(),
      highlightSpecialChars(),
      history(),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      hclLanguage,
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
      EditorView.lineWrapping,
      EditorState.readOnly.of(readOnly),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          onChangeRef.current(update.state.doc.toString())
        }
      }),
    ]
    if (themeMode === "dark") {
      extensions.push(oneDark)
    }
    return extensions
  }, [readOnly, themeMode])

  useEffect(() => {
    if (!containerRef.current) return

    const state = EditorState.create({
      doc: value,
      extensions: createExtensions(),
    })

    const view = new EditorView({
      state,
      parent: containerRef.current,
    })

    viewRef.current = view
    return () => { view.destroy() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [themeMode, readOnly])

  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    const current = view.state.doc.toString()
    if (current !== value) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      })
    }
  }, [value])

  return (
    <div
      ref={containerRef}
      className="overflow-hidden border border-border rounded-md"
      style={{ height: `${height}px` }}
    />
  )
}

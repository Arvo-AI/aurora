/**
 * Code viewer panel for IaC content (Terraform HCL).
 * Uses CodeMirror 6 with syntax highlighting. Read-only display.
 */

import { useRef, useEffect } from "react"
import type { JSX } from "react"
import { EditorState } from "@codemirror/state"
import { EditorView, lineNumbers, highlightSpecialChars } from "@codemirror/view"
import { syntaxHighlighting, HighlightStyle, StreamLanguage } from "@codemirror/language"
import { tags } from "@lezer/highlight"

const matchBlockComment = (stream: { match: (r: RegExp) => unknown; next: () => unknown }) => {
  while (!stream.match(/\*\//)) {
    if (!stream.next()) break
  }
  return "comment" as const
}

const hclLanguage = StreamLanguage.define({
  token(stream) {
    if (stream.match(/\/\/.*/)) return "comment"
    if (stream.match(/\/\*/)) return matchBlockComment(stream)
    if (stream.match(/#.*/)) return "comment"
    if (stream.match(/"(?:[^"\\]|\\.)*"/)) return "string"
    if (stream.match(/<<-?\w+/)) return "string"
    if (stream.match(/\b(resource|provider|variable|output|data|module|terraform|locals|backend|required_providers|required_version)\b/)) return "keyword"
    if (stream.match(/\b(string|number|bool|list|map|set|object|tuple|any)\b/)) return "typeName"
    if (stream.match(/\b(true|false|null)\b/)) return "atom"
    if (stream.match(/\b\d+(\.\d+)?\b/)) return "number"
    if (stream.match(/[{}[\]()=]/)) return "punctuation"
    if (stream.match(/[a-zA-Z_][\w-]*/)) return "variableName"
    stream.next()
    return null
  },
  startState() { return {} },
})

const darkTheme = EditorView.theme({
  "&": { backgroundColor: "#000000", color: "#e0e0e0" },
  ".cm-gutters": { backgroundColor: "#000000", color: "#555", borderRight: "none" },
  ".cm-activeLineGutter": { backgroundColor: "#111" },
  ".cm-activeLine": { backgroundColor: "#0a0a0a" },
  ".cm-selectionBackground": { backgroundColor: "#264f78" },
  "&.cm-focused .cm-selectionBackground": { backgroundColor: "#264f78" },
  ".cm-cursor": { borderLeftColor: "#fff" },
}, { dark: true })

const darkHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: "#c586c0" },
  { tag: tags.string, color: "#ce9178" },
  { tag: tags.comment, color: "#6a9955" },
  { tag: tags.number, color: "#b5cea8" },
  { tag: tags.atom, color: "#569cd6" },
  { tag: tags.typeName, color: "#4ec9b0" },
  { tag: tags.variableName, color: "#9cdcfe" },
  { tag: tags.punctuation, color: "#808080" },
])

interface IaCEditorPanelProps {
  value: string
  height: number
  themeMode: string
}

export const IaCEditorPanel = ({
  value,
  height,
  themeMode,
}: IaCEditorPanelProps): JSX.Element => {
  const containerRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)

  useEffect(() => {
    if (!containerRef.current) return

    const extensions = [
      lineNumbers(),
      highlightSpecialChars(),
      hclLanguage,
      EditorView.lineWrapping,
      EditorState.readOnly.of(true),
      EditorView.editable.of(false),
    ]
    if (themeMode === "dark") {
      extensions.push(darkTheme, syntaxHighlighting(darkHighlight))
    }

    const state = EditorState.create({ doc: value, extensions })
    const view = new EditorView({ state, parent: containerRef.current })
    viewRef.current = view
    return () => { view.destroy() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [themeMode])

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
      className="overflow-auto rounded-md"
      style={{ height: `${height}px` }}
    />
  )
}

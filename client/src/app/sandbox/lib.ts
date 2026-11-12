// Sandbox: types, API calls, and paste classification.
// Pure helpers live here so the page components stay presentational.

export type ArtifactKind = 'log' | 'text' | 'document' | 'image' | 'link';

export interface SandboxArtifact {
  id: string;
  kind: ArtifactKind;
  name: string;
  mimeType: string | null;
  sizeBytes: number;
  createdAt: string;
  preview: string;
  truncated: boolean;
  content?: string;
  description?: 'pending' | 'ready' | 'unavailable';
}

export interface Sandbox {
  id: string;
  name: string;
  publicRepo: string | null;
  incidentTime: string | null;
  createdAt: string;
  updatedAt: string;
  artifactCount: number;
  artifacts?: SandboxArtifact[];
}

export interface SandboxChat {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  status: string | null;
  incidentId: string | null;
  isInvestigation: boolean;
}

export interface ConnectorSuggestion {
  connector: string;
  reason: string;
  artifactCount: number;
}

// ── API ──────────────────────────────────────────────────────────

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { credentials: 'include', ...init });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data as T;
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export const sandboxApi = {
  list: () => call<{ sandboxes: Sandbox[] }>('/api/sandboxes'),
  create: (name: string) => call<{ sandbox: Sandbox }>('/api/sandboxes', json('POST', { name })),
  get: (id: string) => call<{ sandbox: Sandbox }>(`/api/sandboxes/${id}`),
  update: (id: string, patch: Partial<Pick<Sandbox, 'name' | 'publicRepo' | 'incidentTime'>>) =>
    call<{ sandbox: Sandbox }>(`/api/sandboxes/${id}`, json('PATCH', patch)),
  remove: (id: string) => call(`/api/sandboxes/${id}`, { method: 'DELETE' }),
  addText: (id: string, kind: 'log' | 'text' | 'link', content: string, name?: string) =>
    call<{ artifact: SandboxArtifact }>(`/api/sandboxes/${id}/artifacts`, json('POST', { kind, content, name })),
  addFile: (id: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return call<{ artifact: SandboxArtifact }>(`/api/sandboxes/${id}/artifacts`, { method: 'POST', body: form });
  },
  getArtifact: (id: string, artifactId: string) =>
    call<{ artifact: SandboxArtifact }>(`/api/sandboxes/${id}/artifacts/${artifactId}`),
  removeArtifact: (id: string, artifactId: string) =>
    call(`/api/sandboxes/${id}/artifacts/${artifactId}`, { method: 'DELETE' }),
  imageUrl: (id: string, artifactId: string) => `/api/sandboxes/${id}/artifacts/${artifactId}/raw`,
  chats: (id: string) => call<{ chats: SandboxChat[] }>(`/api/sandboxes/${id}/chats`),
  suggestions: (id: string) => call<{ suggestions: ConnectorSuggestion[] }>(`/api/sandboxes/${id}/suggestions`),
  investigate: (id: string, question: string) =>
    call<{ incident_id: string; rca_session_id: string }>(`/api/sandboxes/${id}/investigate`, json('POST', { question })),
  newChat: (id: string, title: string) =>
    call<{ id: string }>('/api/chat-sessions', json('POST', { title, messages: [], sandboxId: id })),
};

// ── Paste classification ─────────────────────────────────────────
// Like Claude's "pasted content": a log-looking or long paste becomes an
// artifact chip instead of flooding the composer.

// No pattern may let a quantifier cross a newline or run unbounded before a
// literal: pasted logs can be 200k chars, and backtracking there is quadratic.
const LOG_SIGNALS: RegExp[] = [
  /^[ \t]*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}/m,                 // ISO timestamp at line start
  /^[ \t]*\[?\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?\]?[ \t]/m,              // time-only prefix
  /^[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}\s/m,                      // syslog "Jun 19 09:26:00"
  /\b(?:ERROR|WARN(?:ING)?|FATAL|CRITICAL|DEBUG|TRACE|PANIC|SEVERE)\b/,   // upper-case levels
  /\blevel=(?:error|warn|warning|info|debug)\b|"(?:level|severity)"\s*:\s*"/i, // logfmt / JSON logs
  /^[ \t]+at [\w$.<>/]{1,300} ?\([^()\n]{0,500}:\d+(?::\d+)?\)[ \t]*$/m, // JS/Java stack frame
  /^Traceback \(most recent call last\):/m,                              // Python traceback
  /^[ \t]*File "[^"\n]{1,500}", line \d+/m,                             // Python frame
  /^(?:goroutine \d+ \[|panic: )/m,                                      // Go panic
  /\b(?:CrashLoopBackOff|OOMKilled|ImagePullBackOff|Back-off restarting|Evicted)\b/, // Kubernetes
  /^NAME\s+READY\s+STATUS\s+RESTARTS\s+AGE/m,                            // kubectl get pods
  /"\s?(?:GET|POST|PUT|DELETE|PATCH) \S+ HTTP\/\d(?:\.\d)?"\s+[1-5]\d\d\b/, // access log
  /^[ \t]*\{[^\n]{0,500}?"(?:timestamp|ts|time|@timestamp)"[ \t]*:/m,  // JSON lines
  /\b(?:Exception|Error):\s.+\n\s+at\s/,                                // exception + frame
  /^##\[(?:error|warning|group)\]/m,                                     // GitHub Actions log
];

const URL_ONLY = /^https?:\/\/\S+$/;
const LONG_CHARS = 800;
const LONG_LINES = 12;

/** How many distinct log signals a text shows. */
export function logScore(text: string): number {
  return LOG_SIGNALS.reduce((n, re) => n + (re.test(text) ? 1 : 0), 0);
}

/**
 * Decide what a paste becomes.
 * - 'link': a single URL
 * - 'log' : at least two log signals, or one signal across 3+ lines
 * - 'text': long (800+ chars or 12+ lines) but not log-like, e.g. a postmortem
 * - null  : short prose; stays in the composer
 */
export function classifyPaste(raw: string): 'link' | 'log' | 'text' | null {
  const text = raw.trim();
  if (!text) return null;
  if (URL_ONLY.test(text)) return 'link';
  const lines = text.split('\n').length;
  const score = logScore(text);
  if (score >= 2 || (score >= 1 && lines >= 3)) return 'log';
  if (text.length >= LONG_CHARS || lines >= LONG_LINES) return 'text';
  return null;
}

/** A readable default name for a pasted artifact. */
export function pasteName(kind: 'log' | 'text', text: string): string {
  const lines = text.trim().split('\n');
  const first = lines.find((l) => l.trim())?.trim().slice(0, 60) || '';
  if (kind === 'log') return `Pasted log · ${lines.length} lines`;
  return first.length > 3 ? first : `Pasted text · ${lines.length} lines`;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

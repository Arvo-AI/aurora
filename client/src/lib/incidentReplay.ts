/** Credential-free incident replay: deterministic input and output validation. */
export const MAX_ALERT_CHARS = 1500;
export const MAX_LOG_CHARS = 12000;
export const MAX_EVIDENCE_LINES = 220;

export type Finding = {
  observation: string;
  evidence_ids: number[];
};
export type ReplayReport = {
  summary: string;
  hypotheses: Finding[];
  observations: Finding[];
  next_checks: string[];
  limitations: string;
  evidence: { id: number; text: string }[];
  evidence_only: true;
};

const secretPatterns = [
  /\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AKIA[0-9A-Z]{16})\b/g,
  /\b(authorization\s*:\s*(?:bearer|basic)\s+)\S+/gi,
  /\b((?:api[_-]?key|password|secret|token)\s*[=:]\s*)["']?[^\s,"']+/gi,
];

export function redactSecrets(input: string): string {
  let result = input;
  for (const pattern of secretPatterns) {
    result = result.replace(pattern, (...args: string[]) => {
      // For assignment-like patterns keep the key label; never disclose the value.
      const prefix = args[1];
      return prefix ? `${prefix}[REDACTED]` : '[REDACTED]';
    });
  }
  return result;
}

export function prepareEvidence(alert: unknown, logs: unknown) {
  if (typeof alert !== 'string' || typeof logs !== 'string') {
    throw new Error('An alert and logs are required.');
  }
  if (!alert.trim() || !logs.trim()) throw new Error('Both fields must contain text.');
  if (alert.length > MAX_ALERT_CHARS || logs.length > MAX_LOG_CHARS) {
    throw new Error('Input too large. Use up to 1,500 alert characters and 12,000 log characters.');
  }
  if (logs.split(/\r?\n/).length > MAX_EVIDENCE_LINES) {
    throw new Error('Too many log lines. Paste at most 220 lines.');
  }
  const lines = redactSecrets(logs).split(/\r?\n/)
    .map(s => s.trim()).filter(Boolean)
    .map((text, i) => ({ id: i + 1, text }));
  return { alert: redactSecrets(alert.trim()), evidence: lines };
}

function strings(value: unknown, max: number): string[] {
  return Array.isArray(value) ? value.filter(x => typeof x === 'string')
    .slice(0, max).map(x => x.slice(0, 500)) : [];
}

function findings(value: unknown, validIds: Set<number>): Finding[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 5).filter(x => x && typeof x.observation === 'string')
    .map(x => ({
      observation: x.observation.slice(0, 600),
      evidence_ids: Array.isArray(x.evidence_ids)
        ? [...new Set<number>(x.evidence_ids.filter((n: unknown) =>
            Number.isInteger(n) && validIds.has(n as number)))].slice(0, 6)
        : [],
    }));
}

export function validateReport(data: unknown, evidence: {id: number; text: string}[]): ReplayReport {
  if (!data || typeof data !== 'object') throw new Error('Invalid model response.');
  const object = data as Record<string, unknown>;
  const validIds = new Set(evidence.map(e => e.id));
  // Only show claims as evidence-backed when they contain validated line references.
  const observations = findings(object.observations, validIds).filter(x => x.evidence_ids.length);
  const hypotheses = findings(object.hypotheses, validIds);
  return {
    summary: typeof object.summary === 'string' ? object.summary.slice(0, 1000) : 'Evidence review completed.',
    observations,
    hypotheses,
    next_checks: strings(object.next_checks, 5),
    limitations: typeof object.limitations === 'string'
      ? object.limitations.slice(0, 600) : 'No live production systems were inspected.',
    evidence,
    evidence_only: true,
  };
}

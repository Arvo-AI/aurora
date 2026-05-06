const FRONT_MATTER_RE = /^---\s*\r?\n[\s\S]*?\r?\n---\s*\r?\n?/;

export function stripFindingsFrontMatter(md: string): string {
  if (!md) return md;
  const s = md.replace(/^[﻿\s]+/, '');
  const m = s.match(FRONT_MATTER_RE);
  return m ? s.slice(m[0].length) : md;
}

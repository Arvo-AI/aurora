function isLeadingSkippable(ch: string): boolean {
  return ch === '﻿' || ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r';
}

export function stripFindingsFrontMatter(md: string): string {
  if (!md) return md;

  let start = 0;
  while (start < md.length && isLeadingSkippable(md[start])) start++;

  if (md.codePointAt(start) !== 45 || md.codePointAt(start + 1) !== 45 || md.codePointAt(start + 2) !== 45) return md;
  const openNl = md.indexOf('\n', start + 3);
  if (openNl < 0 || md.slice(start + 3, openNl).trim() !== '') return md;

  let i = openNl + 1;
  while (i < md.length) {
    const nl = md.indexOf('\n', i);
    const lineEnd = nl < 0 ? md.length : nl;
    if (md.slice(i, lineEnd).trim() === '---') {
      return nl < 0 ? '' : md.slice(nl + 1);
    }
    if (nl < 0) break;
    i = nl + 1;
  }
  return md;
}

import type { Incident } from '@/lib/services/incidents';

/**
 * Root-cause dedup, layer 2: fold recurrences (incidents whose `recurrenceOf`
 * points at an anchor) into one group per anchor for the /incidents list.
 * Pure — no React, no fetch. Mirrors the "Grouping formula" in the design doc
 * root-cause-dedup/02-grouping-ui.md (design-docs repo; not checked in here).
 */

export type IncidentSection = 'investigating' | 'analyzed' | 'merged';

export interface IncidentGroup {
  /** Anchor id (or the incident's own id for a standalone / orphan). */
  id: string;
  anchor: Incident;
  /**
   * Anchor + members, chronological by lastFire. The anchor is usually — not
   * always — first: a member can fire earlier and still fold into a root
   * whose RCA completed later.
   */
  occurrences: Incident[];
  /** occurrences.length */
  occurrenceCount: number;
  /** Newest fire time across the group (epoch ms). */
  lastFiredAt: number;
  section: IncidentSection;
  /** Any occurrence has auroraStatus running/summarizing. */
  investigating: boolean;
}

const STALL_MS = 30 * 60 * 1000;

export function lastFire(incident: Incident): number {
  const t = Date.parse(incident.alertFiredAt ?? incident.startedAt);
  return Number.isNaN(t) ? 0 : t;
}

export function isInvestigating(incident: Incident): boolean {
  return incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing';
}

export function isStalled(incident: Incident, now: number): boolean {
  return incident.status === 'investigating' && now - Date.parse(incident.startedAt) > STALL_MS;
}

/** Time-dependent, so evaluated at render time rather than baked into the group. */
export function isGroupStalled(group: IncidentGroup, now: number): boolean {
  return group.occurrences.some(i => isStalled(i, now));
}

function buildGroup(anchor: Incident, members: Incident[]): IncidentGroup {
  const occurrences = [anchor, ...members].sort((a, b) => lastFire(a) - lastFire(b));

  let section: IncidentSection = 'analyzed';
  if (occurrences.some(i => i.status === 'investigating')) {
    section = 'investigating';
  } else if (members.length === 0 && anchor.status === 'merged') {
    // Only a standalone can sit in Merged; recurrences are never `merged`.
    section = 'merged';
  }

  return {
    id: anchor.id,
    anchor,
    occurrences,
    occurrenceCount: occurrences.length,
    lastFiredAt: lastFire(occurrences[occurrences.length - 1]),
    section,
    investigating: occurrences.some(isInvestigating),
  };
}

export function groupIncidents(incidents: Incident[]): IncidentGroup[] {
  const byId = new Map(incidents.map(i => [i.id, i]));
  const membersByAnchor = new Map<string, Incident[]>();
  const anchors: Incident[] = [];

  for (const incident of incidents) {
    const anchor = incident.recurrenceOf ? byId.get(incident.recurrenceOf) : undefined;
    // A member joins its anchor only when the anchor is in the fetch and is
    // itself top-level; anything else becomes its own row so nothing is hidden.
    if (anchor && !anchor.recurrenceOf) {
      const list = membersByAnchor.get(anchor.id) ?? [];
      list.push(incident);
      membersByAnchor.set(anchor.id, list);
    } else {
      anchors.push(incident);
    }
  }

  return anchors
    .map(anchor => buildGroup(anchor, membersByAnchor.get(anchor.id) ?? []))
    .sort((a, b) => b.lastFiredAt - a.lastFiredAt);
}

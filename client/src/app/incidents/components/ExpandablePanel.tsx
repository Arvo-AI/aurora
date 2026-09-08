'use client';

import { Children, type ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';

interface ExpandablePanelProps {
  open: boolean;
  children: ReactNode;
  /** Classes on the animated outer wrapper (borders, rounding, margins). */
  className?: string;
  /** Classes on the inner content (padding, background). */
  contentClassName?: string;
}

/** Rows past this index share one delay so a long list finishes animating together. */
const MAX_STAGGER_INDEX = 8;

/**
 * Height + opacity expand animation shared by the recurrence group card, the
 * "Fired N times" section and Correlated Alerts. The outer grid animates
 * 0fr -> 1fr, so nothing is measured and content that grows while open is
 * never clipped. `visibility` keeps collapsed links out of the tab order and
 * the accessibility tree (it flips only after the close transition ends).
 * Each direct child staggers in by 50ms.
 */
export default function ExpandablePanel({ open, children, className = '', contentClassName = '' }: Readonly<ExpandablePanelProps>) {
  return (
    <div
      className={`grid overflow-hidden transition-all duration-300 ease-out ${className}`}
      style={{
        gridTemplateRows: open ? '1fr' : '0fr',
        opacity: open ? 1 : 0,
        visibility: open ? 'visible' : 'hidden',
      }}
    >
      <div className="min-h-0 overflow-hidden">
        <div className={contentClassName}>
          {/* Children.map also visits `false`/null slots (`{cond && ...}`); skip them so no empty wrapper is rendered. */}
          {Children.map(children, (child, index) => child == null ? null : (
            <div
              style={{ transitionDelay: open ? `${Math.min(index, MAX_STAGGER_INDEX) * 50}ms` : '0ms' }}
              className={`transition-all duration-300 ease-out ${
                open ? 'opacity-100 translate-y-0' : 'opacity-0 -translate-y-2'
              }`}
            >
              {child}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function ExpandChevron({ open, className = '' }: Readonly<{ open: boolean; className?: string }>) {
  return (
    <ChevronDown
      className={`h-4 w-4 transition-transform duration-300 ease-out ${open ? 'rotate-180' : ''} ${className}`}
    />
  );
}

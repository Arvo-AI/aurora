'use client';

import { useMemo, useEffect, useRef } from 'react';
import { Workflow } from 'lucide-react';

export interface ActionItem {
  id: string;
  name: string;
}

interface SlashCommandMenuProps {
  input: string;
  actions: ActionItem[];
  onSelect: (action: ActionItem) => void;
  highlightedIndex: number;
}

export default function SlashCommandMenu({ input, actions, onSelect, highlightedIndex }: SlashCommandMenuProps) {
  const match = input.match(/^\/actions?\s*(.*)/i);
  const query = match?.[1]?.toLowerCase() ?? '';
  const listRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    if (!query) return actions;
    return actions.filter(a => a.name.toLowerCase().includes(query));
  }, [actions, query]);

  useEffect(() => {
    const el = listRef.current?.children[highlightedIndex + 1] as HTMLElement | undefined;
    el?.scrollIntoView({ block: 'nearest' });
  }, [highlightedIndex]);

  if (!match) return null;

  return (
    <div ref={listRef} className="absolute bottom-full left-0 right-0 mb-2 bg-zinc-900 border border-zinc-700/60 rounded-lg shadow-xl overflow-hidden z-50 max-h-48 overflow-y-auto">
      <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-zinc-500 font-medium border-b border-zinc-800/60">
        Actions
      </div>
      {actions.length === 0 ? (
        <div className="px-3 py-3 text-xs text-zinc-500">No actions configured. Create one in Settings.</div>
      ) : filtered.length === 0 ? (
        <div className="px-3 py-3 text-xs text-zinc-500">No matching actions</div>
      ) : (
        filtered.slice(0, 8).map((action, i) => (
          <button
            key={action.id}
            type="button"
            onMouseDown={e => { e.preventDefault(); onSelect(action); }}
            className={`w-full flex items-center gap-2 px-3 py-2 text-sm text-zinc-300 transition-colors text-left ${
              i === highlightedIndex ? 'bg-zinc-800' : 'hover:bg-zinc-800/50'
            }`}
          >
            <Workflow className="h-3.5 w-3.5 text-zinc-500 flex-shrink-0" />
            <span className="truncate">{action.name}</span>
          </button>
        ))
      )}
    </div>
  );
}

export function getFilteredActions(input: string, actions: ActionItem[]): ActionItem[] {
  const match = input.match(/^\/actions?\s*(.*)/i);
  if (!match) return [];
  const query = match[1]?.toLowerCase() ?? '';
  if (!query) return actions;
  return actions.filter(a => a.name.toLowerCase().includes(query));
}

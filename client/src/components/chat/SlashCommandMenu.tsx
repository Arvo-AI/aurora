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
  onCommandSelect: () => void;
  highlightedIndex: number;
  stage: 'command' | 'action';
}

export default function SlashCommandMenu({ input, actions, onSelect, onCommandSelect, highlightedIndex, stage }: SlashCommandMenuProps) {
  const listRef = useRef<HTMLDivElement>(null);

  const query = useMemo(() => {
    const idx = input.search(/\/actions?\s/i);
    if (idx === -1) return '';
    const afterCmd = input.slice(idx).replace(/^\/actions?\s/i, '');
    return afterCmd.toLowerCase();
  }, [input]);

  const filtered = useMemo(() => {
    if (stage === 'command') return [];
    if (!query) return actions;
    return actions.filter(a => a.name.toLowerCase().includes(query));
  }, [actions, query, stage]);

  useEffect(() => {
    const offset = 1;
    const el = listRef.current?.children[highlightedIndex + offset] as HTMLElement | undefined;
    el?.scrollIntoView({ block: 'nearest' });
  }, [highlightedIndex, stage]);

  return (
    <div ref={listRef} className="absolute bottom-full left-0 right-0 mb-2 bg-zinc-900 border border-zinc-700/60 rounded-lg shadow-xl overflow-hidden z-50 max-h-48 overflow-y-auto">
      <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-zinc-500 font-medium border-b border-zinc-800/60">
        {stage === 'command' ? 'Commands' : 'Actions'}
      </div>
      {stage === 'command' ? (
        <button
          type="button"
          onMouseDown={e => { e.preventDefault(); onCommandSelect(); }}
          className={`w-full flex items-center gap-3 px-3 py-2 text-sm transition-colors text-left ${
            highlightedIndex === 0 ? 'bg-zinc-800' : 'hover:bg-zinc-800/50'
          }`}
        >
          <Workflow className="h-3.5 w-3.5 text-zinc-500 flex-shrink-0" />
          <div>
            <span className="text-zinc-200 font-medium">/action</span>
            <span className="text-zinc-500 ml-2">Run a configured action</span>
          </div>
        </button>
      ) : actions.length === 0 ? (
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
  const idx = input.search(/\/actions?\s/i);
  if (idx !== -1) {
    const query = input.slice(idx).replace(/^\/actions?\s/i, '').toLowerCase();
    if (!query) return actions;
    return actions.filter(a => a.name.toLowerCase().includes(query));
  }
  return actions;
}

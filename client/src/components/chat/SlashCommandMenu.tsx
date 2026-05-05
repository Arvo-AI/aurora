'use client';

import { useMemo } from 'react';
import { Workflow } from 'lucide-react';

interface ActionItem {
  id: string;
  name: string;
}

interface SlashCommandMenuProps {
  input: string;
  actions: ActionItem[];
  onSelect: (action: ActionItem) => void;
}

export default function SlashCommandMenu({ input, actions, onSelect }: SlashCommandMenuProps) {
  const match = input.match(/^\/actions?\s*(.*)/i);
  const query = match?.[1]?.toLowerCase() ?? '';

  const filtered = useMemo(() => {
    if (!query) return actions;
    return actions.filter(a => a.name.toLowerCase().includes(query));
  }, [actions, query]);

  if (!match) return null;

  return (
    <div className="absolute bottom-full left-0 right-0 mb-2 bg-zinc-900 border border-zinc-700/60 rounded-lg shadow-xl overflow-hidden z-50 max-h-48 overflow-y-auto">
      <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-zinc-500 font-medium border-b border-zinc-800/60">
        Actions
      </div>
      {actions.length === 0 ? (
        <div className="px-3 py-3 text-xs text-zinc-500">No actions configured. Create one in Settings.</div>
      ) : filtered.length === 0 ? (
        <div className="px-3 py-3 text-xs text-zinc-500">No matching actions</div>
      ) : (
        filtered.slice(0, 8).map(action => (
          <button
            key={action.id}
            type="button"
            onMouseDown={e => { e.preventDefault(); onSelect(action); }}
            className="w-full flex items-center gap-2 px-3 py-2 text-sm text-zinc-300 hover:bg-zinc-800 transition-colors text-left"
          >
            <Workflow className="h-3.5 w-3.5 text-zinc-500 flex-shrink-0" />
            <span className="truncate">{action.name}</span>
          </button>
        ))
      )}
    </div>
  );
}

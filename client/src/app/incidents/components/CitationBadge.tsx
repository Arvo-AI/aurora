'use client';

interface CitationBadgeProps {
  citationKey: string;
  onClick: () => void;
}

export default function CitationBadge({ citationKey, onClick }: CitationBadgeProps) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="inline-flex items-center justify-center min-w-[1.5rem] h-5 px-1.5 mx-0.5 text-xs font-medium rounded bg-blue-500/20 text-blue-400 border border-blue-500/30 hover:bg-blue-500/30 hover:text-blue-300 transition-colors cursor-pointer align-baseline"
      title={`View evidence [${citationKey}]`}
      aria-label={`View citation ${citationKey}`}
    >
      [{citationKey}]
    </button>
  );
}

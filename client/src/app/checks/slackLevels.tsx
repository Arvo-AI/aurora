'use client';

export type Level = 'off' | 'listen' | 'sure' | 'free';

interface LevelInfo {
  readonly key: Level;
  readonly name: string;
  readonly means: string;
}

/** Quietest first. Each level does what the one before it does, and more. */
export const LEVELS: readonly LevelInfo[] = [
  { key: 'off', name: 'Off', means: 'Aurora does not read the channel. It still answers when someone @mentions it.' },
  { key: 'listen', name: 'Listening', means: 'Aurora reads the channel and looks into problems people mention. It never posts there.' },
  { key: 'sure', name: 'Speaks when sure', means: 'Aurora also replies in the thread, only once it has confirmed that something is broken.' },
  { key: 'free', name: 'Speaks freely', means: 'Aurora also answers questions, and says what it checked when it found nothing wrong.' },
];

/** The dial: one choice out of the four levels. */
export function LevelDial({ value, onChange, disabled, label, compact }: {
  readonly value: Level;
  readonly onChange: (level: Level) => void;
  readonly disabled?: boolean;
  readonly label: string;
  readonly compact?: boolean;
}) {
  const size = compact ? 'px-2.5 py-1 text-[11px]' : 'px-4 py-2 text-sm';
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="inline-flex max-w-full overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950/60 p-0.5"
    >
      {LEVELS.map((level) => {
        const selected = level.key === value;
        return (
          <button
            key={level.key}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            title={level.means}
            onClick={() => !selected && onChange(level.key)}
            className={`${size} rounded-md font-medium whitespace-nowrap transition-colors disabled:cursor-not-allowed ${
              selected
                ? 'bg-zinc-100 text-zinc-900'
                : 'text-zinc-400 hover:text-zinc-200 disabled:hover:text-zinc-400'
            }`}
          >
            {level.name}
          </button>
        );
      })}
    </div>
  );
}

function Said({ who, children }: { readonly who: string; readonly children: React.ReactNode }) {
  return (
    <div className="flex gap-2.5">
      <div className="h-6 w-6 shrink-0 rounded bg-zinc-800 text-[10px] font-semibold text-zinc-400 flex items-center justify-center uppercase">
        {who[0]}
      </div>
      <div className="min-w-0">
        <p className="text-xs font-semibold text-zinc-300">{who}</p>
        <p className="text-sm text-zinc-300">{children}</p>
      </div>
    </div>
  );
}

function AuroraReply({ children }: { readonly children: React.ReactNode }) {
  return (
    <div className="ml-8 mt-2 border-l-2 border-sky-500/60 pl-3">
      <p className="text-xs font-semibold text-sky-400">Aurora <span className="font-normal text-zinc-600">in the thread</span></p>
      <p className="text-sm text-zinc-300">{children}</p>
    </div>
  );
}

function Quiet({ children }: { readonly children: React.ReactNode }) {
  return <p className="ml-8 mt-1.5 text-xs text-zinc-600">{children}</p>;
}

const PROBLEM_REPLY = 'checkout has 0 of 3 pods ready since 14:02. The 14:01 deploy is crash-looping on a missing variable. I opened an incident and I am investigating.';

/** The same two messages in a channel, and what Aurora does about each at *level*. */
export function LevelPreview({ level }: { readonly level: Level }) {
  const problem: Record<Level, React.ReactNode> = {
    off: <Quiet>Aurora does not see this.</Quiet>,
    listen: <Quiet>Aurora checks checkout without posting. It is down, so an incident opens and the on-call is told the usual way.</Quiet>,
    sure: <AuroraReply>{PROBLEM_REPLY}</AuroraReply>,
    free: <AuroraReply>{PROBLEM_REPLY}</AuroraReply>,
  };
  const question: Record<Level, React.ReactNode> = {
    off: <Quiet>Aurora does not see this.</Quiet>,
    listen: <Quiet>Not a problem, so Aurora does nothing.</Quiet>,
    sure: <Quiet>Not a confirmed problem, so Aurora stays quiet.</Quiet>,
    free: <AuroraReply>It runs on prod-east, in the shop namespace.</AuroraReply>,
  };
  return (
    <div className="rounded-lg border border-zinc-800/80 bg-zinc-950/40 p-4">
      <p className="text-[11px] uppercase tracking-wide text-zinc-600 mb-3">What that looks like in a channel</p>
      <div className="space-y-4">
        <div>
          <Said who="Maya">is checkout timing out for anyone else?</Said>
          {problem[level]}
        </div>
        <div>
          <Said who="Sam">which cluster does checkout run on again?</Said>
          {question[level]}
        </div>
      </div>
    </div>
  );
}

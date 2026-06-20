'use client';

import { Citation } from '@/lib/services/incidents';
import { ChevronDown } from 'lucide-react';
import React, { useState, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

function parseRuledOutItems(content: string | undefined) {
  if (!content) return [];
  return content
    .split(/(?:^|\n)[ \t]*[-*][ \t]+/)
    .filter(s => s.trim())
    .map(item => {
      const cleaned = item.replace(/^\*\*/, '').trim();
      const dashSplit = /^(.+?)\s*[—–]\s*([\s\S]+)$/.exec(cleaned);
      if (dashSplit) {
        const title = dashSplit[1].replaceAll('**', '').trim();
        const explanation = dashSplit[2].trim();
        return { title, explanation };
      }
      return { title: cleaned.replaceAll('**', ''), explanation: '' };
    });
}

function renderRuledOutItemText(
  str: string,
  citations: Citation[],
  onCitationClick: (c: Citation) => void,
) {
  const parts = str.split(/(`[^`]+`|\[\d+(?:,\s*\d+)*\])/g);
  return parts.map((part, i) => {
    if (!part) return null;
    const codeMatch = /^`([^`]+)`$/.exec(part);
    if (codeMatch) return <code key={`code-${i}`} className="font-mono text-[0.86em] bg-white/[.055] text-[#AEB4BE] border border-white/[.06] rounded-[6px] px-1.5 py-px whitespace-nowrap">{codeMatch[1]}</code>;
    const citeMatch = /^\[(\d+(?:,\s*\d+)*)\]$/.exec(part);
    if (citeMatch) {
      const keys = citeMatch[1].split(/,\s*/);
      return (<span key={`cites-${i}`}>{keys.map(key => {
        const citation = citations.find(c => c.key === key);
        return citation ? (
          <button key={`cite-${key}`} onClick={() => onCitationClick(citation)} className="font-mono text-[10px] text-zinc-500 border border-white/[.07] rounded-[5px] px-1.5 py-px mx-0.5 hover:text-emerald-400 hover:border-emerald-400/30 transition-colors">{key}</button>
        ) : <span key={`cite-${key}`} className="font-mono text-[10px] text-zinc-500 border border-white/[.07] rounded-[5px] px-1.5 py-px mx-0.5">{key}</span>;
      })}</span>);
    }
    return <span key={`text-${i}`}>{part}</span>;
  });
}

export default function RuledOutConsole({ text, citations, onCitationClick }: {
  readonly text: string;
  readonly citations: Citation[];
  readonly onCitationClick: (c: Citation) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const sections = useMemo(() => {
    const ruledOutMatch = /##\s*Ruled Out\s*\n([\s\S]*?)(?=##\s*Not Checked|$)/.exec(text);
    const notCheckedMatch = /##\s*Not Checked\s*\n([\s\S]*?)$/.exec(text);
    return {
      ruledOut: parseRuledOutItems(ruledOutMatch?.[1]),
      notChecked: parseRuledOutItems(notCheckedMatch?.[1]),
    };
  }, [text]);

  const renderItemText = (str: string) => renderRuledOutItemText(str, citations, onCitationClick);

  if (sections.ruledOut.length === 0 && sections.notChecked.length === 0) return null;

  return (
    <div className="mt-4 rounded-[14px] border border-white/[.07] overflow-hidden bg-[#0A0C0F]">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-4 py-3 bg-[#0D0F13] hover:bg-[#10131a] transition-colors"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-zinc-500" />
        <span className="text-[13px] font-semibold text-zinc-400 flex-1 text-left">
          Ruled Out &amp; Not Checked{' '}
          <span className="ml-2 text-[11px] font-normal text-zinc-500">
            {sections.ruledOut.length} eliminated · {sections.notChecked.length} skipped
          </span>
        </span>
        <motion.span animate={{ rotate: expanded ? 0 : -90 }} transition={{ duration: 0.2 }}>
          <ChevronDown className="w-3.5 h-3.5 text-zinc-500" />
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="ruled-out-content"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ height: { duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }, opacity: { duration: 0.2, delay: 0.05 } }}
            className="overflow-hidden"
          >
            <div className="border-t border-white/[.07]">
              {sections.ruledOut.length > 0 && (
                <>
                  <div className="px-4 pt-3 pb-1.5">
                    <span className="text-[10.5px] tracking-[.05em] uppercase text-zinc-600 font-medium">Ruled Out</span>
                  </div>
                  {sections.ruledOut.map((item) => (
                    <div key={item.title} className="px-4 py-2.5 border-t border-white/[.035] first:border-t-0">
                      <p className="text-[12.5px] font-medium text-zinc-300">{renderItemText(item.title)}</p>
                      {item.explanation && <p className="text-[12px] text-zinc-500 mt-1 leading-relaxed">{renderItemText(item.explanation)}</p>}
                    </div>
                  ))}
                </>
              )}
              {sections.notChecked.length > 0 && (
                <>
                  <div className={`px-4 pt-3 pb-1.5 ${sections.ruledOut.length > 0 ? 'border-t border-white/[.07]' : ''}`}>
                    <span className="text-[10.5px] tracking-[.05em] uppercase text-amber-500/70 font-medium">Not Checked</span>
                  </div>
                  {sections.notChecked.map((item) => (
                    <div key={item.title} className="px-4 py-2.5 border-t border-white/[.035] first:border-t-0">
                      <p className="text-[12.5px] font-medium text-zinc-300">{renderItemText(item.title)}</p>
                      {item.explanation && <p className="text-[12px] text-zinc-500 mt-1 leading-relaxed">{renderItemText(item.explanation)}</p>}
                    </div>
                  ))}
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

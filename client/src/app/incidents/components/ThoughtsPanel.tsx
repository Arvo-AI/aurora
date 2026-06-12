'use client';

import { useState, useCallback, useEffect, useRef, type PointerEvent as ReactPointerEvent } from 'react';
import { StreamingThought, Incident } from '@/lib/services/incidents';
import SubAgentInvestigationsSection from '@/app/incidents/components/SubAgentInvestigationsSection';

const PANEL_WIDTH_STORAGE_KEY = 'thoughts-panel-width';
export const PANEL_WIDTH_DEFAULT = 400;
const PANEL_WIDTH_MIN = 320;
const PANEL_WIDTH_MAX_RATIO = 0.8;

function readStoredPanelWidth(): number {
  if (typeof window === 'undefined') return PANEL_WIDTH_DEFAULT;
  const raw = window.localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
  const parsed = raw ? Number(raw) : Number.NaN;
  return Number.isFinite(parsed) && parsed >= PANEL_WIDTH_MIN ? parsed : PANEL_WIDTH_DEFAULT;
}

function clampPanelWidth(value: number): number {
  if (typeof window === 'undefined') return Math.max(PANEL_WIDTH_MIN, value);
  const max = Math.max(PANEL_WIDTH_MIN, Math.floor(window.innerWidth * PANEL_WIDTH_MAX_RATIO));
  return Math.max(PANEL_WIDTH_MIN, Math.min(value, max));
}

interface ThoughtsPanelProps {
  thoughts: StreamingThought[];
  incident: Incident;
  isVisible: boolean;
  onWidthChange?: (width: number) => void;
}

export default function ThoughtsPanel({ thoughts, incident, isVisible, onWidthChange }: ThoughtsPanelProps) {
  const [hasSubAgentFindings, setHasSubAgentFindings] = useState(false);
  const [panelWidth, setPanelWidth] = useState<number>(PANEL_WIDTH_DEFAULT);
  const panelWidthRef = useRef<number>(PANEL_WIDTH_DEFAULT);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  useEffect(() => {
    panelWidthRef.current = panelWidth;
  }, [panelWidth]);

  useEffect(() => {
    setPanelWidth(clampPanelWidth(readStoredPanelWidth()));
  }, []);

  useEffect(() => {
    onWidthChange?.(panelWidth);
  }, [panelWidth, onWidthChange]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onWindowResize = () => setPanelWidth((w) => clampPanelWidth(w));
    window.addEventListener('resize', onWindowResize);
    return () => window.removeEventListener('resize', onWindowResize);
  }, []);

  const handleResizeStart = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    resizeStateRef.current = { startX: e.clientX, startWidth: panelWidthRef.current };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = (ev: PointerEvent) => {
      const state = resizeStateRef.current;
      if (!state) return;
      setPanelWidth(clampPanelWidth(state.startWidth + (state.startX - ev.clientX)));
    };
    const cleanup = () => {
      resizeStateRef.current = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', cleanup);
      window.removeEventListener('pointercancel', cleanup);
      try { window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(panelWidthRef.current)); } catch { /* ignore quota errors */ }
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', cleanup);
    window.addEventListener('pointercancel', cleanup);
  }, []);

  if (incident.status === 'merged') return null;
  if (!isVisible) return null;

  return (
    <div
      className="fixed top-[49px] right-0 h-[calc(100vh-49px)] bg-background z-20 border-l border-zinc-800/50 flex flex-col"
      style={{ width: panelWidth }}
    >
      <div
        role="separator"
        aria-label="Resize thoughts panel"
        aria-orientation="vertical"
        onPointerDown={handleResizeStart}
        className="absolute left-0 top-0 h-full w-1.5 -translate-x-1/2 cursor-col-resize bg-transparent hover:bg-orange-500/40 transition-colors z-30"
      />
      {/* Header */}
      <div className="flex items-center border-b border-zinc-800/50 bg-zinc-900/50 px-4 h-10 shrink-0">
        <span className="text-sm text-white font-medium">
          Thoughts
          {(incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing') && <span className="ml-1.5 w-2 h-2 bg-orange-400 rounded-full animate-pulse inline-block" />}
        </span>
      </div>

      {/* Thoughts View */}
      <div className="flex-1 relative overflow-hidden">
        <div className="absolute inset-0 overflow-y-auto p-5">
          <div className="space-y-4">
            {thoughts.map((thought) => (
              <div key={thought.id} className="pl-4 border-l-2 border-zinc-700 hover:border-orange-500/50 transition-colors">
                <div className="text-xs text-zinc-500 mb-1">
                  {new Date(thought.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </div>
                <p className="text-sm text-zinc-300">{thought.content}</p>
              </div>
            ))}
            {(incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing') && (
              <div className="pl-4 border-l-2 border-orange-500/50">
                <div className="flex items-center gap-2 text-sm text-zinc-400">
                  <div className="flex gap-1">
                    <span className="w-1.5 h-1.5 bg-orange-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="w-1.5 h-1.5 bg-orange-400 rounded-full animate-bounce" style={{ animationDelay: '100ms' }} />
                    <span className="w-1.5 h-1.5 bg-orange-400 rounded-full animate-bounce" style={{ animationDelay: '200ms' }} />
                  </div>
                  <span>{incident.auroraStatus === 'summarizing' ? 'Generating summary...' : 'Thinking...'}</span>
                </div>
              </div>
            )}
            {thoughts.length === 0 && !hasSubAgentFindings && incident.auroraStatus !== 'running' && incident.auroraStatus !== 'summarizing' && (
              <p className="text-center text-zinc-500 text-sm py-8">No investigation thoughts yet</p>
            )}
            <SubAgentInvestigationsSection
              incidentId={incident.id}
              isActive={incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing'}
              onHasFindings={setHasSubAgentFindings}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

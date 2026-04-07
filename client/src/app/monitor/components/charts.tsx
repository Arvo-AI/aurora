'use client';

import { ReactNode, useMemo, useState, useCallback } from 'react';
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
  ReferenceArea,
  TooltipProps,
} from 'recharts';
import { Loader2, ZoomIn, RotateCcw } from 'lucide-react';

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------

export const CHART_COLORS = [
  '#3b82f6', '#8b5cf6', '#06b6d4', '#10b981',
  '#f59e0b', '#ef4444', '#ec4899',
];

export const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ef4444', high: '#f97316', medium: '#eab308',
  low: '#3b82f6', unknown: '#6b7280',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export type Period = '7d' | '30d' | '90d';

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return 'N/A';
  if (seconds < 0) return '0s';
  if (seconds > 0 && seconds < 1) return '<1s';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (mins < 60) return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remainMins = mins % 60;
  if (hours < 24) return remainMins > 0 ? `${hours}h ${remainMins}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const remainHours = hours % 24;
  return remainHours > 0 ? `${days}d ${remainHours}h` : `${days}d`;
}

export function formatDate(dateStr: string): string {
  const parts = dateStr.split('-').map(Number);
  const d = parts.length === 3 && parts.every(n => Number.isFinite(n))
    ? new Date(parts[0], parts[1] - 1, parts[2])
    : new Date(dateStr);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export function formatCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toFixed(n % 1 === 0 ? 0 : 2);
}

export function formatCost(n: number): string {
  if (n >= 1) return `$${n.toFixed(2)}`;
  if (n >= 0.01) return `$${n.toFixed(3)}`;
  if (n > 0) return `$${n.toFixed(4)}`;
  return '$0.00';
}

function autoFormatAxis(n: number): string {
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  if (Math.abs(n) >= 1) return n % 1 === 0 ? String(n) : n.toFixed(1);
  if (n === 0) return '0';
  return n.toFixed(2);
}

function measureYAxisWidth(
  data: Record<string, unknown>[],
  seriesKeys: string[],
  formatter?: (v: number) => string,
): number {
  const fmt = formatter || autoFormatAxis;
  let maxLen = 2;
  for (const row of data) {
    for (const k of seriesKeys) {
      const v = row[k];
      if (typeof v === 'number') {
        const label = fmt(v);
        if (label.length > maxLen) maxLen = label.length;
      }
    }
  }
  return Math.max(40, Math.min(maxLen * 8 + 12, 90));
}

// ---------------------------------------------------------------------------
// Period Selector
// ---------------------------------------------------------------------------

const PERIODS: { label: string; value: Period }[] = [
  { label: '7d', value: '7d' },
  { label: '30d', value: '30d' },
  { label: '90d', value: '90d' },
];

export function PeriodSelector({ value, onChange }: { value: Period; onChange: (p: Period) => void }) {
  return (
    <div className="flex rounded-lg border border-zinc-800 overflow-hidden">
      {PERIODS.map(p => (
        <button
          key={p.value}
          onClick={() => onChange(p.value)}
          className={`px-3 py-1 text-xs font-medium transition-all duration-200 ${
            value === p.value
              ? 'bg-zinc-700/80 text-zinc-100'
              : 'text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60'
          }`}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chart Panel wrapper
// ---------------------------------------------------------------------------

export function ChartPanel({
  title, subtitle, loading, children, className,
}: {
  title: string;
  subtitle?: string;
  loading?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-5 ${className ?? ''}`}>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-medium text-zinc-300">{title}</h3>
          {subtitle && <p className="text-xs text-zinc-500 mt-0.5">{subtitle}</p>}
        </div>
        {loading && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-600" />}
      </div>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stat Card
// ---------------------------------------------------------------------------

export function StatCard({
  label, value, sub, icon: Icon,
}: {
  label: string;
  value: string;
  sub?: string;
  icon?: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-4 hover:ring-1 hover:ring-white/5 transition-all duration-200">
      <div className="flex items-center gap-2 mb-2">
        {Icon && <Icon className="h-3.5 w-3.5 text-zinc-500" />}
        <span className="text-xs text-zinc-500 font-medium uppercase tracking-wider">{label}</span>
      </div>
      <p className="text-3xl font-semibold tracking-tight text-zinc-100" style={{ fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </p>
      {sub && <p className="text-xs text-zinc-500 mt-1">{sub}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton loader for charts
// ---------------------------------------------------------------------------

export function ChartSkeleton({ height = 280 }: { height?: number }) {
  return (
    <div className="animate-pulse" style={{ height }}>
      <div className="bg-zinc-800/50 rounded-lg w-full h-full" />
    </div>
  );
}

export function StatCardSkeleton() {
  return (
    <div className="bg-zinc-900/60 border border-zinc-800/80 rounded-xl p-4 animate-pulse">
      <div className="h-3 w-20 bg-zinc-800/50 rounded mb-3" />
      <div className="h-8 w-24 bg-zinc-800/50 rounded mb-2" />
      <div className="h-3 w-16 bg-zinc-800/50 rounded" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty State
// ---------------------------------------------------------------------------

export function EmptyState({
  icon: Icon, message, hint,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  message: string;
  hint?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {Icon && <Icon className="h-8 w-8 text-zinc-700 mb-3" />}
      <p className="text-sm text-zinc-500">{message}</p>
      {hint && <p className="text-xs text-zinc-600 mt-1">{hint}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Custom Tooltip
// ---------------------------------------------------------------------------

interface GrafanaTooltipEntry { dataKey: string; color: string; value: number; name: string }

function GrafanaTooltipContent({
  active, payload, label, formatter,
}: TooltipProps<number, string> & { formatter?: (v: number, key: string) => string }) {
  if (!active || !payload?.length) return null;
  const entries = (payload as GrafanaTooltipEntry[]).filter(e => e.value !== undefined && e.value !== null);
  if (!entries.length) return null;
  return (
    <div className="bg-zinc-950/95 backdrop-blur-sm border border-zinc-700/50 rounded-lg shadow-2xl px-3 py-2.5 pointer-events-none">
      <p className="text-xs text-zinc-400 mb-1.5 font-medium">
        {typeof label === 'string' && label.match(/^\d{4}-\d{2}/) ? formatDate(label) : label}
      </p>
      {entries.map((entry, i) => (
        <div key={i} className="flex items-center justify-between gap-6 text-xs leading-5">
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: entry.color }} />
            <span className="text-zinc-400">{entry.name || entry.dataKey}</span>
          </div>
          <span className="text-zinc-200 font-medium" style={{ fontVariantNumeric: 'tabular-nums' }}>
            {formatter ? formatter(entry.value, entry.dataKey) : autoFormatAxis(entry.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Zoom reset button
// ---------------------------------------------------------------------------

function ZoomResetButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="absolute top-2 right-2 z-10 flex items-center gap-1 px-2 py-1 rounded-md bg-zinc-800/90 border border-zinc-700/50 text-[10px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700/90 transition-all duration-150"
    >
      <RotateCcw className="h-3 w-3" />
      Reset zoom
    </button>
  );
}

function ZoomHint() {
  return (
    <div className="absolute bottom-1 right-2 z-10 flex items-center gap-1 text-[10px] text-zinc-600 pointer-events-none select-none">
      <ZoomIn className="h-3 w-3" />
      Drag to zoom
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared chart props
// ---------------------------------------------------------------------------

const GRID_STROKE = '#27272a';
const AXIS_TICK = { fill: '#71717a', fontSize: 11 };
const AXIS_STROKE = '#27272a';
const ANIMATION_DURATION = 600;
const ANIMATION_EASING = 'ease-out' as const;
const CURSOR_STYLE = { stroke: '#52525b', strokeDasharray: '4 4' };

const BRUSH_STYLE = {
  height: 24,
  fill: '#18181b',
  stroke: '#3f3f46',
  travellerWidth: 8,
};

// ---------------------------------------------------------------------------
// useChartZoom — drag-to-zoom state for any chart type
// ---------------------------------------------------------------------------

function useChartZoom(data: Record<string, unknown>[], xKey: string) {
  const [left, setLeft] = useState<string | null>(null);
  const [right, setRight] = useState<string | null>(null);
  const [refLeft, setRefLeft] = useState('');
  const [refRight, setRefRight] = useState('');
  const [zoomed, setZoomed] = useState(false);
  const [startIdx, setStartIdx] = useState(0);
  const [endIdx, setEndIdx] = useState(data.length - 1);

  const handleMouseDown = useCallback((e: { activeLabel?: string }) => {
    if (e?.activeLabel) setRefLeft(e.activeLabel);
  }, []);

  const handleMouseMove = useCallback((e: { activeLabel?: string }) => {
    if (refLeft && e?.activeLabel) setRefRight(e.activeLabel);
  }, [refLeft]);

  const handleMouseUp = useCallback(() => {
    if (refLeft && refRight && refLeft !== refRight) {
      const leftIdx = data.findIndex(d => String(d[xKey]) === refLeft);
      const rightIdx = data.findIndex(d => String(d[xKey]) === refRight);
      if (leftIdx >= 0 && rightIdx >= 0) {
        const lo = Math.min(leftIdx, rightIdx);
        const hi = Math.max(leftIdx, rightIdx);
        setStartIdx(lo);
        setEndIdx(hi);
        setLeft(String(data[lo][xKey]));
        setRight(String(data[hi][xKey]));
        setZoomed(true);
      }
    }
    setRefLeft('');
    setRefRight('');
  }, [data, xKey, refLeft, refRight]);

  const handleReset = useCallback(() => {
    setLeft(null);
    setRight(null);
    setStartIdx(0);
    setEndIdx(data.length - 1);
    setZoomed(false);
  }, [data.length]);

  const zoomedData = zoomed ? data.slice(startIdx, endIdx + 1) : data;

  return {
    zoomedData,
    zoomed,
    refLeft: refLeft && refRight && refLeft !== refRight ? refLeft : '',
    refRight: refLeft && refRight && refLeft !== refRight ? refRight : '',
    handleMouseDown,
    handleMouseMove,
    handleMouseUp,
    handleReset,
    startIdx,
    endIdx,
  };
}

// ---------------------------------------------------------------------------
// GrafanaAreaChart
// ---------------------------------------------------------------------------

export interface AreaSeries {
  key: string;
  name?: string;
  color?: string;
  stacked?: boolean;
}

export function GrafanaAreaChart({
  data, series, height = 280, xKey = 'date', xFormatter, yFormatter, tooltipFormatter, yAxisWidth,
}: {
  data: Record<string, unknown>[];
  series: AreaSeries[];
  height?: number;
  xKey?: string;
  xFormatter?: (v: string) => string;
  yFormatter?: (v: number) => string;
  tooltipFormatter?: (v: number, key: string) => string;
  yAxisWidth?: number;
}) {
  const {
    zoomedData, zoomed, refLeft, refRight,
    handleMouseDown, handleMouseMove, handleMouseUp, handleReset,
  } = useChartZoom(data, xKey);

  const gradients = useMemo(
    () => series.map((s, i) => ({ id: `grad-${s.key}`, color: s.color || CHART_COLORS[i % CHART_COLORS.length] })),
    [series],
  );

  const yWidth = yAxisWidth ?? measureYAxisWidth(zoomedData, series.map(s => s.key), yFormatter);

  if (!data.length) return null;

  return (
    <div className="relative select-none">
      {zoomed && <ZoomResetButton onClick={handleReset} />}
      {!zoomed && data.length > 3 && <ZoomHint />}
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart
          data={zoomedData}
          margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
          onMouseDown={handleMouseDown as unknown as (e: unknown) => void}
          onMouseMove={handleMouseMove as unknown as (e: unknown) => void}
          onMouseUp={handleMouseUp}
        >
          <defs>
            {gradients.map(g => (
              <linearGradient key={g.id} id={g.id} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={g.color} stopOpacity={0.35} />
                <stop offset="100%" stopColor={g.color} stopOpacity={0.02} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} vertical={false} />
          <XAxis
            dataKey={xKey}
            tickFormatter={xFormatter || formatDate}
            tick={AXIS_TICK}
            stroke={AXIS_STROKE}
            tickLine={false}
            axisLine={false}
            minTickGap={40}
          />
          <YAxis
            allowDecimals={false}
            tick={AXIS_TICK}
            stroke={AXIS_STROKE}
            tickLine={false}
            axisLine={false}
            tickFormatter={yFormatter || autoFormatAxis}
            width={yWidth}
          />
          <Tooltip
            cursor={CURSOR_STYLE}
            content={<GrafanaTooltipContent formatter={tooltipFormatter} />}
          />
          <Legend
            wrapperStyle={{ fontSize: 11, color: '#71717a', paddingTop: 8 }}
            iconType="circle"
            iconSize={8}
          />
          {series.map((s, i) => (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name || s.key}
              stackId={s.stacked !== false ? '1' : undefined}
              stroke={s.color || CHART_COLORS[i % CHART_COLORS.length]}
              fill={`url(#grad-${s.key})`}
              strokeWidth={1.5}
              dot={false}
              activeDot={{ r: 3, strokeWidth: 0 }}
              animationDuration={ANIMATION_DURATION}
              animationEasing={ANIMATION_EASING}
            />
          ))}
          {refLeft && refRight && (
            <ReferenceArea x1={refLeft} x2={refRight} strokeOpacity={0.3} fill="#3b82f6" fillOpacity={0.15} />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// GrafanaBarChart
// ---------------------------------------------------------------------------

export interface BarSeries {
  key: string;
  name?: string;
  color?: string;
  stacked?: boolean;
  radius?: [number, number, number, number];
}

export function GrafanaBarChart({
  data, series, height = 280, xKey = 'name', xFormatter, yFormatter, tooltipFormatter, layout,
}: {
  data: Record<string, unknown>[];
  series: BarSeries[];
  height?: number;
  xKey?: string;
  xFormatter?: (v: string) => string;
  yFormatter?: (v: number) => string;
  tooltipFormatter?: (v: number, key: string) => string;
  layout?: 'horizontal' | 'vertical';
}) {
  const gradients = useMemo(
    () => series.map((s, i) => ({ id: `bar-grad-${s.key}`, color: s.color || CHART_COLORS[i % CHART_COLORS.length] })),
    [series],
  );

  if (!data.length) return null;

  const isVertical = layout === 'vertical';
  const yWidth = isVertical ? 100 : measureYAxisWidth(data, series.map(s => s.key), yFormatter);

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout={isVertical ? 'vertical' : 'horizontal'} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <defs>
          {gradients.map(g => (
            <linearGradient key={g.id} id={g.id} x1="0" y1="0" x2={isVertical ? '1' : '0'} y2={isVertical ? '0' : '1'}>
              <stop offset="0%" stopColor={g.color} stopOpacity={0.9} />
              <stop offset="100%" stopColor={g.color} stopOpacity={0.5} />
            </linearGradient>
          ))}
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} horizontal={!isVertical} vertical={isVertical} />
        {isVertical ? (
          <>
            <XAxis type="number" tick={AXIS_TICK} stroke={AXIS_STROKE} tickLine={false} axisLine={false} tickFormatter={yFormatter || autoFormatAxis} />
            <YAxis type="category" dataKey={xKey} tick={AXIS_TICK} stroke={AXIS_STROKE} tickLine={false} axisLine={false} width={yWidth} tickFormatter={xFormatter} />
          </>
        ) : (
          <>
            <XAxis dataKey={xKey} tick={AXIS_TICK} stroke={AXIS_STROKE} tickLine={false} axisLine={false} tickFormatter={xFormatter} minTickGap={30} />
            <YAxis allowDecimals={false} tick={AXIS_TICK} stroke={AXIS_STROKE} tickLine={false} axisLine={false} tickFormatter={yFormatter || autoFormatAxis} width={yWidth} />
          </>
        )}
        <Tooltip cursor={{ fill: 'rgba(255,255,255,0.03)' }} content={<GrafanaTooltipContent formatter={tooltipFormatter} />} />
        <Legend wrapperStyle={{ fontSize: 11, color: '#71717a', paddingTop: 8 }} iconType="circle" iconSize={8} />
        {series.map((s, i) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            name={s.name || s.key}
            stackId={s.stacked ? '1' : undefined}
            fill={`url(#bar-grad-${s.key})`}
            radius={s.radius || [4, 4, 0, 0]}
            animationDuration={ANIMATION_DURATION}
            animationEasing={ANIMATION_EASING}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// GrafanaLineChart
// ---------------------------------------------------------------------------

export interface LineSeries {
  key: string;
  name?: string;
  color?: string;
  dashed?: boolean;
}

export function GrafanaLineChart({
  data, series, height = 280, xKey = 'date', xFormatter, yFormatter, tooltipFormatter, yAxisWidth,
}: {
  data: Record<string, unknown>[];
  series: LineSeries[];
  height?: number;
  xKey?: string;
  xFormatter?: (v: string) => string;
  yFormatter?: (v: number) => string;
  tooltipFormatter?: (v: number, key: string) => string;
  yAxisWidth?: number;
}) {
  const {
    zoomedData, zoomed, refLeft, refRight,
    handleMouseDown, handleMouseMove, handleMouseUp, handleReset,
  } = useChartZoom(data, xKey);

  const yWidth = yAxisWidth ?? measureYAxisWidth(zoomedData, series.map(s => s.key), yFormatter);

  if (!data.length) return null;

  return (
    <div className="relative select-none">
      {zoomed && <ZoomResetButton onClick={handleReset} />}
      {!zoomed && data.length > 3 && <ZoomHint />}
      <ResponsiveContainer width="100%" height={height}>
        <LineChart
          data={zoomedData}
          margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
          onMouseDown={handleMouseDown as unknown as (e: unknown) => void}
          onMouseMove={handleMouseMove as unknown as (e: unknown) => void}
          onMouseUp={handleMouseUp}
        >
          <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} vertical={false} />
          <XAxis dataKey={xKey} tickFormatter={xFormatter || formatDate} tick={AXIS_TICK} stroke={AXIS_STROKE} tickLine={false} axisLine={false} minTickGap={40} />
          <YAxis allowDecimals={false} tick={AXIS_TICK} stroke={AXIS_STROKE} tickLine={false} axisLine={false} tickFormatter={yFormatter || autoFormatAxis} width={yWidth} />
          <Tooltip cursor={CURSOR_STYLE} content={<GrafanaTooltipContent formatter={tooltipFormatter} />} />
          <Legend wrapperStyle={{ fontSize: 11, color: '#71717a', paddingTop: 8 }} iconType="circle" iconSize={8} />
          {series.map((s, i) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name || s.key}
              stroke={s.color || CHART_COLORS[i % CHART_COLORS.length]}
              strokeWidth={1.5}
              strokeDasharray={s.dashed ? '6 3' : undefined}
              dot={false}
              activeDot={{ r: 3, strokeWidth: 0 }}
              animationDuration={ANIMATION_DURATION}
              animationEasing={ANIMATION_EASING}
            />
          ))}
          {refLeft && refRight && (
            <ReferenceArea x1={refLeft} x2={refRight} strokeOpacity={0.3} fill="#3b82f6" fillOpacity={0.15} />
          )}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

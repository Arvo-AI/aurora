'use client';

import { useState } from 'react';
import { Radar, DollarSign, HeartPulse, Timer, ShieldCheck } from 'lucide-react';
import { PeriodSelector, type Period } from './components/charts';
import FleetTab from './components/fleet-tab';
import UsageTab from './components/usage-tab';
import SreTab from './components/sre-tab';
import WaterfallTab from './components/waterfall-tab';
import AuditTab from './components/audit-tab';

const TABS = [
  { id: 'fleet', label: 'Fleet', icon: Radar },
  { id: 'usage', label: 'Usage & Cost', icon: DollarSign },
  { id: 'sre', label: 'SRE Metrics', icon: HeartPulse },
  { id: 'waterfall', label: 'Execution', icon: Timer },
  { id: 'audit', label: 'Audit Log', icon: ShieldCheck },
] as const;

type TabId = typeof TABS[number]['id'];

export default function MonitorPage() {
  const [tab, setTab] = useState<TabId>('fleet');
  const [period, setPeriod] = useState<Period>('30d');

  return (
    <div className="max-w-[1400px] mx-auto py-8 px-4 sm:px-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-100">Monitor</h1>
          <p className="text-sm text-zinc-500 mt-1">Observability, cost tracking, and compliance</p>
        </div>
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-1 border-b border-zinc-800/80 mb-6">
        {TABS.map(t => {
          const Icon = t.icon;
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium transition-all duration-200 border-b-2 -mb-px ${
                active
                  ? 'border-zinc-100 text-zinc-100'
                  : 'border-transparent text-zinc-500 hover:text-zinc-300 hover:border-zinc-700'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      <div>
        {tab === 'fleet' && <FleetTab period={period} />}
        {tab === 'usage' && <UsageTab period={period} />}
        {tab === 'sre' && <SreTab period={period} />}
        {tab === 'waterfall' && <WaterfallTab period={period} />}
        {tab === 'audit' && <AuditTab period={period} />}
      </div>
    </div>
  );
}

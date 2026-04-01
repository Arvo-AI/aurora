'use client';

import { useState, useEffect, useCallback } from 'react';
import { useSystemHealthStream, SystemHealthData } from '@/hooks/useSystemHealthStream';

type Tab = 'fleet' | 'waterfall' | 'health';

export default function MonitorPage() {
  const [activeTab, setActiveTab] = useState<Tab>('fleet');

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <h1 className="text-2xl font-bold">Monitor (PR 3 temp page)</h1>
      <div className="flex gap-2 border-b pb-2">
        {(['fleet', 'waterfall', 'health'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setActiveTab(t)}
            className={`px-4 py-2 rounded-t text-sm font-medium ${
              activeTab === t ? 'bg-white border border-b-0 text-black' : 'text-gray-500 hover:text-gray-700'
            }`}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      {activeTab === 'fleet' && <FleetSection />}
      {activeTab === 'waterfall' && <WaterfallSection />}
      {activeTab === 'health' && <HealthSection />}
    </div>
  );
}

/* ---------- Fleet Tab ---------- */
function FleetSection() {
  const [fleet, setFleet] = useState<Record<string, unknown>[] | null>(null);
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [activity, setActivity] = useState<Record<string, unknown>[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchFleet = useCallback(async () => {
    try {
      const [fleetRes, summaryRes] = await Promise.all([
        fetch('/api/monitor/fleet'),
        fetch('/api/monitor/fleet/summary'),
      ]);
      if (fleetRes.ok) setFleet(await fleetRes.json());
      else setError(`Fleet: ${fleetRes.status}`);
      if (summaryRes.ok) setSummary(await summaryRes.json());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { fetchFleet(); }, [fetchFleet]);

  const loadActivity = async (incidentId: string) => {
    setSelectedId(incidentId);
    try {
      const res = await fetch(`/api/monitor/fleet/${incidentId}/activity`);
      if (res.ok) setActivity(await res.json());
    } catch (e) {
      setActivity(null);
    }
  };

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">Fleet Summary</h2>
      {summary && <JsonBlock data={summary} />}
      {error && <p className="text-red-500">{error}</p>}

      <h2 className="text-lg font-semibold">Agent Runs</h2>
      {fleet && fleet.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="min-w-full text-xs border">
            <thead>
              <tr className="bg-gray-100">
                {Object.keys(fleet[0]).map((k) => (
                  <th key={k} className="px-2 py-1 border text-left">{k}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {fleet.map((row, i) => (
                <tr
                  key={i}
                  onClick={() => loadActivity(String(row.incident_id))}
                  className={`cursor-pointer hover:bg-blue-50 ${
                    selectedId === String(row.incident_id) ? 'bg-blue-100' : ''
                  }`}
                >
                  {Object.values(row).map((v, j) => (
                    <td key={j} className="px-2 py-1 border whitespace-nowrap">{String(v ?? '')}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-gray-400 text-sm">No agent runs found</p>
      )}

      {selectedId && (
        <>
          <h2 className="text-lg font-semibold">Activity for {selectedId}</h2>
          {activity ? <JsonBlock data={activity} /> : <p className="text-gray-400 text-sm">Loading...</p>}
        </>
      )}
    </div>
  );
}

/* ---------- Waterfall Tab ---------- */
function WaterfallSection() {
  const [incidentId, setIncidentId] = useState('');
  const [waterfall, setWaterfall] = useState<Record<string, unknown> | null>(null);
  const [toolPerf, setToolPerf] = useState<Record<string, unknown>[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadWaterfall = async () => {
    if (!incidentId.trim()) return;
    setError(null);
    try {
      const res = await fetch(`/api/monitor/incidents/${incidentId}/waterfall`);
      if (res.ok) setWaterfall(await res.json());
      else setError(`Waterfall: ${res.status}`);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    fetch('/api/monitor/tools/performance')
      .then((r) => r.ok ? r.json() : null)
      .then((d) => d && setToolPerf(d))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">Execution Waterfall</h2>
      <div className="flex gap-2 items-center">
        <input
          value={incidentId}
          onChange={(e) => setIncidentId(e.target.value)}
          placeholder="Paste incident ID..."
          className="border rounded px-3 py-1 text-sm w-96"
        />
        <button onClick={loadWaterfall} className="bg-blue-600 text-white px-3 py-1 rounded text-sm">Load</button>
      </div>
      {error && <p className="text-red-500 text-sm">{error}</p>}

      {waterfall && (
        <>
          <div className="text-sm text-gray-600">
            Steps: {String((waterfall as Record<string, unknown>).total_steps)} |
            Duration: {String((waterfall as Record<string, unknown>).total_duration_ms)}ms |
            Errors: {String((waterfall as Record<string, unknown>).error_count)}
          </div>
          {Array.isArray((waterfall as Record<string, unknown>).steps) && (
            <div className="overflow-x-auto">
              <table className="min-w-full text-xs border">
                <thead>
                  <tr className="bg-gray-100">
                    <th className="px-2 py-1 border">step</th>
                    <th className="px-2 py-1 border">tool</th>
                    <th className="px-2 py-1 border">status</th>
                    <th className="px-2 py-1 border">duration_ms</th>
                    <th className="px-2 py-1 border">error</th>
                  </tr>
                </thead>
                <tbody>
                  {((waterfall as Record<string, unknown>).steps as Record<string, unknown>[]).map((s, i) => (
                    <tr key={i} className={s.status === 'error' ? 'bg-red-50' : ''}>
                      <td className="px-2 py-1 border">{String(s.step_index)}</td>
                      <td className="px-2 py-1 border">{String(s.tool_name)}</td>
                      <td className="px-2 py-1 border">{String(s.status)}</td>
                      <td className="px-2 py-1 border">{String(s.duration_ms ?? '-')}</td>
                      <td className="px-2 py-1 border text-red-600">{String(s.error_message ?? '')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <h2 className="text-lg font-semibold mt-6">Tool Performance (last 7d)</h2>
      {toolPerf && toolPerf.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="min-w-full text-xs border">
            <thead>
              <tr className="bg-gray-100">
                {Object.keys(toolPerf[0]).map((k) => (
                  <th key={k} className="px-2 py-1 border text-left">{k}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {toolPerf.map((row, i) => (
                <tr key={i}>
                  {Object.values(row).map((v, j) => (
                    <td key={j} className="px-2 py-1 border">{String(v ?? '')}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-gray-400 text-sm">No tool performance data yet</p>
      )}
    </div>
  );
}

/* ---------- Health Tab ---------- */
function HealthSection() {
  const { health, isConnected, error, eventLog } = useSystemHealthStream();

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
        <span className="text-sm text-gray-600">{isConnected ? 'Connected' : 'Disconnected'}</span>
        {error && <span className="text-red-500 text-sm ml-2">{error}</span>}
      </div>

      <h2 className="text-lg font-semibold">Latest Health Snapshot</h2>
      {health ? <JsonBlock data={health} /> : <p className="text-gray-400 text-sm">Waiting for first event...</p>}

      <h2 className="text-lg font-semibold">Event Log ({eventLog.length} events)</h2>
      <div className="max-h-96 overflow-y-auto border rounded p-2 bg-gray-50 text-xs font-mono space-y-1">
        {eventLog.length === 0 && <p className="text-gray-400">No events yet</p>}
        {[...eventLog].reverse().map((ev, i) => (
          <div key={i} className="border-b pb-1">
            <span className="text-gray-500">[{ev.timestamp}]</span>{' '}
            {Object.entries(ev.services || {}).map(([name, svc]) => (
              <span key={name} className={`mr-2 ${svc.status === 'healthy' ? 'text-green-700' : 'text-red-700'}`}>
                {name}:{svc.status}
              </span>
            ))}
            <span className="text-blue-700">
              workers:{ev.celery?.worker_count} active:{ev.celery?.active_tasks} queue:{ev.celery?.queue_depth ?? '?'}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- Shared ---------- */
function JsonBlock({ data }: { data: unknown }) {
  return (
    <pre className="bg-gray-50 border rounded p-3 text-xs overflow-x-auto max-h-80 overflow-y-auto">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

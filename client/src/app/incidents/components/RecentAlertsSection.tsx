'use client';

import { useState, useEffect, useRef } from 'react';
import { RecentIncident, incidentsService } from '@/lib/services/incidents';
import { ChevronDown, Clock, Server, ArrowRight, Loader2, Check, X } from 'lucide-react';
import Image from 'next/image';

interface RecentAlertsSectionProps {
  currentIncidentId: string;
  onAlertMerged?: () => void;
}

function RecentAlertCard({ 
  incident, 
  onMerge,
  isMerging,
}: { 
  incident: RecentIncident; 
  onMerge: () => void;
  isMerging: boolean;
}) {
  const [showConfirm, setShowConfirm] = useState(false);

  return (
    <div className="group relative p-3 rounded-lg bg-zinc-900/30 border border-zinc-800/50 hover:border-zinc-700/50 transition-all duration-200">
      <div className="flex items-start gap-3">
        {/* Source icon */}
        <div className="flex-shrink-0 mt-0.5 opacity-50">
          <Image 
            src={incident.sourceType === 'pagerduty' ? '/pagerduty-icon.svg' : `/${incident.sourceType}.svg`}
            alt={incident.sourceType}
            width={16}
            height={16}
            className="object-contain"
          />
        </div>
        
        {/* Content */}
        <div className="flex-1 min-w-0">
          <h4 className="text-sm text-zinc-400 truncate">
            {incident.alertTitle}
          </h4>
          
          <div className="flex items-center gap-2 mt-1 text-xs text-zinc-600">
            <div className="flex items-center gap-1">
              <Server className="w-3 h-3" />
              <span>{incident.alertService}</span>
            </div>
            <span>•</span>
            <div className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              <span>{incidentsService.formatTimeAgo(incident.createdAt)}</span>
            </div>
          </div>
        </div>

        {/* Merge button / Confirmation */}
        <div className="flex-shrink-0">
          {showConfirm ? (
            <div className="flex items-center gap-1">
              <button
                onClick={() => {
                  onMerge();
                  setShowConfirm(false);
                }}
                disabled={isMerging}
                className="p-1.5 rounded bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 transition-colors disabled:opacity-50"
                title="Confirm merge"
              >
                {isMerging ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Check className="w-3.5 h-3.5" />
                )}
              </button>
              <button
                onClick={() => setShowConfirm(false)}
                disabled={isMerging}
                className="p-1.5 rounded bg-zinc-700/50 hover:bg-zinc-700 text-zinc-400 transition-colors disabled:opacity-50"
                title="Cancel"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowConfirm(true)}
              className="opacity-0 group-hover:opacity-100 flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 transition-all"
              title="Link this alert to current incident"
            >
              <span>Link here</span>
              <ArrowRight className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default function RecentAlertsSection({ 
  currentIncidentId,
  onAlertMerged,
}: RecentAlertsSectionProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [recentIncidents, setRecentIncidents] = useState<RecentIncident[]>([]);
  const [loading, setLoading] = useState(false);
  const [mergingId, setMergingId] = useState<string | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [contentHeight, setContentHeight] = useState(0);
  const hasFetched = useRef(false);

  // Fetch recent incidents when expanded
  useEffect(() => {
    if (isExpanded && !hasFetched.current) {
      hasFetched.current = true;
      setLoading(true);
      incidentsService.getRecentUnlinkedIncidents(currentIncidentId)
        .then(setRecentIncidents)
        .finally(() => setLoading(false));
    }
  }, [isExpanded, currentIncidentId]);

  // Measure content height
  useEffect(() => {
    if (contentRef.current) {
      setContentHeight(contentRef.current.scrollHeight);
    }
  }, [recentIncidents, isExpanded, loading]);

  const handleMerge = async (sourceIncidentId: string) => {
    setMergingId(sourceIncidentId);
    const result = await incidentsService.mergeAlertToIncident(currentIncidentId, sourceIncidentId);
    setMergingId(null);
    
    if (result.success) {
      // Remove the merged incident from the list
      setRecentIncidents(prev => prev.filter(i => i.id !== sourceIncidentId));
      onAlertMerged?.();
    }
  };

  // Don't render if no incidents after loading
  if (hasFetched.current && !loading && recentIncidents.length === 0) {
    return null;
  }

  return (
    <div className="mt-4">
      {/* Header button - more subtle than correlated alerts */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className={`w-full flex items-center justify-between px-3 py-2 rounded-lg border transition-all duration-200 
          bg-zinc-900/20 border-zinc-800/30 hover:bg-zinc-900/40 hover:border-zinc-700/50
          ${isExpanded ? 'rounded-b-none border-b-0' : ''}`}
      >
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-500">
            Other recent alerts
          </span>
          {!hasFetched.current && (
            <span className="text-[10px] text-zinc-600">(click to load)</span>
          )}
        </div>
        
        <ChevronDown 
          className={`w-3.5 h-3.5 text-zinc-600 transition-transform duration-300 ease-out ${
            isExpanded ? 'rotate-180' : ''
          }`} 
        />
      </button>
      
      {/* Expandable content */}
      <div 
        className={`overflow-hidden transition-all duration-300 ease-out border-zinc-800/30
          ${isExpanded ? 'border border-t-0 rounded-b-lg bg-zinc-900/10' : ''}`}
        style={{ 
          maxHeight: isExpanded ? `${contentHeight + 24}px` : '0px',
          opacity: isExpanded ? 1 : 0,
        }}
      >
        <div ref={contentRef} className="p-2 space-y-1.5">
          {loading ? (
            <div className="flex items-center justify-center py-4">
              <Loader2 className="w-4 h-4 animate-spin text-zinc-600" />
            </div>
          ) : recentIncidents.length === 0 ? (
            <p className="text-xs text-zinc-600 text-center py-3">
              No other recent alerts
            </p>
          ) : (
            <>
              <p className="text-[10px] text-zinc-600 px-1 mb-2">
                Click "Link here" to merge an alert into this investigation
              </p>
              {recentIncidents.map((incident, index) => (
                <div
                  key={incident.id}
                  style={{
                    transitionDelay: isExpanded ? `${index * 30}ms` : '0ms',
                  }}
                  className={`transition-all duration-200 ease-out ${
                    isExpanded 
                      ? 'opacity-100 translate-y-0' 
                      : 'opacity-0 -translate-y-1'
                  }`}
                >
                  <RecentAlertCard 
                    incident={incident}
                    onMerge={() => handleMerge(incident.id)}
                    isMerging={mergingId === incident.id}
                  />
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

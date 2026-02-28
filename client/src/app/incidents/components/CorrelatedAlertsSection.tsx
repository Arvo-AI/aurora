'use client';

import { useState, useRef, useEffect } from 'react';
import { CorrelatedAlert, incidentsService } from '@/lib/services/incidents';
import { ChevronDown, Link2, Clock, Server, Zap, Target, Type } from 'lucide-react';
import Image from 'next/image';

interface CorrelatedAlertsSectionProps {
  alerts: CorrelatedAlert[];
}

function getStrategyIcon(strategy: string) {
  switch (strategy) {
    case 'topology':
      return <Target className="w-3 h-3" />;
    case 'time_window':
      return <Clock className="w-3 h-3" />;
    case 'similarity':
      return <Type className="w-3 h-3" />;
    default:
      return <Zap className="w-3 h-3" />;
  }
}

function getStrategyLabel(strategy: string) {
  switch (strategy) {
    case 'topology':
      return 'Same Service';
    case 'time_window':
      return 'Time Proximity';
    case 'similarity':
      return 'Title Match';
    default:
      return strategy;
  }
}

function getStrategyExplanation(alert: CorrelatedAlert): string {
  const details = alert.correlationDetails || {};
  const parts: string[] = [];
  
  if (details.topology && details.topology > 0) {
    if (details.topology === 1) {
      parts.push(`same service (${alert.alertService})`);
    } else {
      parts.push(`related service (${Math.round(details.topology * 100)}% match)`);
    }
  }
  
  if (details.time_window && details.time_window > 0) {
    parts.push(`within time window (${Math.round(details.time_window * 100)}% proximity)`);
  }
  
  if (details.similarity && details.similarity > 0) {
    parts.push(`similar title (${Math.round(details.similarity * 100)}% match)`);
  }
  
  if (parts.length === 0) {
    return `Correlated with ${Math.round((alert.correlationScore || 0) * 100)}% confidence`;
  }
  
  return parts.join(', ');
}

function CorrelatedAlertCard({ alert, isNew }: { alert: CorrelatedAlert; isNew: boolean }) {
  const scorePercent = Math.round((alert.correlationScore || 0) * 100);
  
  return (
    <div className={`group relative p-4 rounded-lg border transition-all duration-300 ${
      isNew 
        ? 'bg-amber-500/5 border-amber-500/30 animate-pulse-once' 
        : 'bg-zinc-900/50 border-zinc-800 hover:border-zinc-700'
    }`}>
      {/* New badge */}
      {isNew && (
        <div className="absolute -top-2 -right-2 px-2 py-0.5 rounded-full bg-amber-500 text-[10px] font-bold text-black uppercase tracking-wider">
          New
        </div>
      )}
      
      <div className="flex items-start gap-3">
        {/* Source icon */}
        <div className="flex-shrink-0 mt-0.5">
          <Image 
            src={alert.sourceType === 'pagerduty' ? '/pagerduty-icon.svg' : alert.sourceType === 'dynatrace' ? '/dynatrace.png' : `/${alert.sourceType}.svg`}
            alt={alert.sourceType}
            width={18}
            height={18}
            className={`object-contain opacity-70${alert.sourceType === 'dynatrace' ? ' scale-[2.2]' : ''}${alert.sourceType === 'bigpanda' ? ' bg-white rounded-sm p-0.5' : ''}`}
          />
        </div>
        
        {/* Content */}
        <div className="flex-1 min-w-0">
          {/* Title */}
          <h4 className="text-sm font-medium text-zinc-200 truncate">
            {alert.alertTitle}
          </h4>
          
          {/* Metadata row */}
          <div className="flex items-center gap-3 mt-1.5 text-xs text-zinc-500">
            <div className="flex items-center gap-1">
              <Server className="w-3 h-3" />
              <span className="text-zinc-400">{alert.alertService}</span>
            </div>
            <div className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              <span>{incidentsService.formatTimeAgo(alert.receivedAt)}</span>
            </div>
          </div>
          
          {/* Correlation reasoning */}
          <div className="mt-2 flex items-center gap-2">
            <div className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-[11px] font-medium ${
              scorePercent >= 80 
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' 
                : scorePercent >= 60 
                  ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                  : 'bg-zinc-800 text-zinc-400 border border-zinc-700'
            }`}>
              {getStrategyIcon(alert.correlationStrategy)}
              <span>{getStrategyLabel(alert.correlationStrategy)}</span>
              <span className="opacity-60">•</span>
              <span>{scorePercent}%</span>
            </div>
          </div>
          
          {/* Detailed explanation */}
          <p className="mt-2 text-[11px] text-zinc-500 leading-relaxed">
            {getStrategyExplanation(alert)}
          </p>
        </div>
      </div>
    </div>
  );
}

export default function CorrelatedAlertsSection({ alerts }: CorrelatedAlertsSectionProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const [contentHeight, setContentHeight] = useState(0);
  
  // Filter out primary alerts (those with strategy 'primary' or score of 1)
  const correlatedAlerts = alerts.filter(a => a.correlationStrategy !== 'primary');
  
  // Measure content height when alerts change or expansion state changes
  useEffect(() => {
    if (contentRef.current) {
      setContentHeight(contentRef.current.scrollHeight);
    }
  }, [correlatedAlerts, isExpanded]);
  
  if (correlatedAlerts.length === 0) {
    return null;
  }
  
  // Helper to safely parse date, fallback to 0 for invalid dates
  const safeParseDate = (dateString: string | null | undefined): number => {
    if (!dateString) return 0;
    const timestamp = new Date(dateString).getTime();
    return isNaN(timestamp) ? 0 : timestamp;
  };
  
  // Sort by receivedAt descending (newest first)
  const sortedAlerts = [...correlatedAlerts].sort(
    (a, b) => safeParseDate(b.receivedAt) - safeParseDate(a.receivedAt)
  );
  
  // Check if any alert arrived in the last 30 seconds (for "new" badge)
  const now = Date.now();
  const isRecent = (receivedAt: string | null | undefined) => {
    const timestamp = safeParseDate(receivedAt);
    return timestamp > 0 && now - timestamp < 30000;
  };
  
  const hasNewAlerts = sortedAlerts.some(a => isRecent(a.receivedAt));

  return (
    <div className="mt-6">
      {/* Header button */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className={`w-full flex items-center justify-between px-4 py-3 rounded-lg border transition-all duration-200 ${
          hasNewAlerts 
            ? 'bg-amber-500/5 border-amber-500/30 hover:bg-amber-500/10' 
            : 'bg-zinc-900/50 border-zinc-800 hover:bg-zinc-800/50 hover:border-zinc-700'
        } ${isExpanded ? 'rounded-b-none border-b-0' : ''}`}
      >
        <div className="flex items-center gap-3">
          <div className={`p-1.5 rounded-md transition-colors duration-200 ${hasNewAlerts ? 'bg-amber-500/20' : 'bg-zinc-800'}`}>
            <Link2 className={`w-4 h-4 transition-colors duration-200 ${hasNewAlerts ? 'text-amber-400' : 'text-zinc-400'}`} />
          </div>
          <div className="text-left">
            <div className="flex items-center gap-2">
              <span className={`text-sm font-medium transition-colors duration-200 ${hasNewAlerts ? 'text-amber-300' : 'text-zinc-300'}`}>
                Correlated Alerts
              </span>
              <span className={`px-1.5 py-0.5 rounded text-xs font-medium transition-colors duration-200 ${
                hasNewAlerts 
                  ? 'bg-amber-500/20 text-amber-400' 
                  : 'bg-zinc-800 text-zinc-400'
              }`}>
                {correlatedAlerts.length}
              </span>
              {hasNewAlerts && (
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500"></span>
                </span>
              )}
            </div>
            <p className="text-xs text-zinc-500 mt-0.5">
              {hasNewAlerts 
                ? 'New related alerts detected'
                : 'Related alerts'
              }
            </p>
          </div>
        </div>
        
        <div className={`p-1 rounded transition-all duration-200 ${isExpanded ? 'bg-zinc-700' : 'hover:bg-zinc-800'}`}>
          <ChevronDown 
            className={`w-4 h-4 text-zinc-400 transition-transform duration-300 ease-out ${
              isExpanded ? 'rotate-180' : ''
            }`} 
          />
        </div>
      </button>
      
      {/* Expandable content with smooth height animation */}
      <div 
        className={`overflow-hidden transition-all duration-300 ease-out ${
          hasNewAlerts 
            ? 'border-amber-500/30' 
            : 'border-zinc-800'
        } ${isExpanded ? 'border border-t-0 rounded-b-lg' : ''}`}
        style={{ 
          maxHeight: isExpanded ? `${contentHeight + 24}px` : '0px',
          opacity: isExpanded ? 1 : 0,
        }}
      >
        <div 
          ref={contentRef}
          className={`p-3 space-y-2 ${
            hasNewAlerts ? 'bg-amber-500/[0.02]' : 'bg-zinc-900/30'
          }`}
        >
          {sortedAlerts.map((alert, index) => (
            <div
              key={alert.id}
              style={{
                transitionDelay: isExpanded ? `${index * 50}ms` : '0ms',
              }}
              className={`transition-all duration-300 ease-out ${
                isExpanded 
                  ? 'opacity-100 translate-y-0' 
                  : 'opacity-0 -translate-y-2'
              }`}
            >
              <CorrelatedAlertCard 
                alert={alert} 
                isNew={isRecent(alert.receivedAt)}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

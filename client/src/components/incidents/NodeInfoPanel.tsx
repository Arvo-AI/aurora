'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import { Panel } from '@xyflow/react';
import { InfraNode, NodeStatus } from '@/types/visualization';
import { Incident, Citation } from '@/lib/services/incidents';
import {
  X, Send, Loader2, AlertTriangle, Terminal,
  Container, Layers, Network, Database, Server, Zap, HardDrive, Archive,
  Grid3x3, FolderTree, MapPin, Bell, Activity, Boxes, LucideIcon, Shield,
  MessageSquare
} from 'lucide-react';

interface NodeInfoPanelProps {
  node: InfraNode & { isRootCause?: boolean; isAffected?: boolean };
  incident: Incident;
  citations: Citation[];
  isFullscreen: boolean;
  onClose: () => void;
}

interface LocalMessage {
  role: 'user' | 'assistant';
  text: string;
}

const statusColors: Record<NodeStatus, { border: string; bg: string; label: string }> = {
  healthy: { border: '#22c55e', bg: '#052e16', label: 'Healthy' },
  degraded: { border: '#eab308', bg: '#422006', label: 'Degraded' },
  failed: { border: '#ef4444', bg: '#450a0a', label: 'Failed' },
  investigating: { border: '#f97316', bg: '#431407', label: 'Investigating' },
  unknown: { border: '#71717a', bg: '#18181b', label: 'Unknown' },
};

function getIconForType(type: string): LucideIcon | null {
  const iconMap: Record<string, LucideIcon> = {
    pod: Container, deployment: Layers, service: Network, statefulset: Database, daemonset: Grid3x3, replicaset: Layers,
    vm: Server, instance: Server, lambda: Zap, 'cloud-function': Zap, node: HardDrive,
    'load-balancer': Network, ingress: Network, 'api-gateway': Network,
    database: Database, postgres: Database, mysql: Database, mongodb: Database, redis: Database, elasticsearch: Database,
    bucket: Archive, pvc: HardDrive, queue: Activity,
    cluster: Boxes, namespace: FolderTree, region: MapPin,
    alert: Bell, event: Activity, metric: Activity,
  };
  return iconMap[type.toLowerCase()] || null;
}

function matchesNode(text: string, nodeLabel: string): boolean {
  return text.toLowerCase().includes(nodeLabel.toLowerCase());
}

export default function NodeInfoPanel({ node, incident, citations, isFullscreen, onClose }: NodeInfoPanelProps) {
  const [chatInput, setChatInput] = useState('');
  const [localMessages, setLocalMessages] = useState<LocalMessage[]>([]);
  const [isWaiting, setIsWaiting] = useState(false);
  const [sessionMsgCount, setSessionMsgCount] = useState<number | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Track session ID in a ref so polling survives prop changes mid-conversation
  const activeSessionIdRef = useRef<string | undefined>(incident.chatSessionId);
  const isWaitingRef = useRef(false);
  const prevNodeIdRef = useRef<string>(node.id);

  // Keep refs in sync without triggering re-renders
  useEffect(() => { isWaitingRef.current = isWaiting; }, [isWaiting]);
  useEffect(() => {
    if (incident.chatSessionId) activeSessionIdRef.current = incident.chatSessionId;
  }, [incident.chatSessionId]);

  const Icon = getIconForType(node.type);
  const colors = statusColors[node.status];
  const label = node.label;

  // Match incident data against node label
  const matchedAlerts = (incident.correlatedAlerts || []).filter(
    a => matchesNode(a.alertTitle, label) || matchesNode(a.alertService, label)
  );
  const matchedCitations = citations.filter(
    c => matchesNode(c.command, label) || matchesNode(c.output, label)
  );
  const matchedSuggestions = (incident.suggestions || []).filter(
    s => matchesNode(s.title, label) || matchesNode(s.description, label) || (s.command && matchesNode(s.command, label))
  );

  // Scroll to bottom of messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [localMessages]);

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
    };
  }, []);

  const stopPolling = useCallback(() => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  }, []);

  // Polling uses session ID passed as argument, not from closure/prop
  const startPolling = useCallback((sid: string, baseCount: number) => {
    stopPolling();
    if (!sid) return;

    pollIntervalRef.current = setInterval(async () => {
      try {
        const res = await fetch(`/api/chat-sessions/${sid}`);
        if (!res.ok) return;
        const data = await res.json();
        const messages = data.messages || [];

        if (messages.length > baseCount) {
          const newMessages = messages.slice(baseCount);
          const assistantMsg = newMessages.find((m: { sender: string }) => m.sender === 'assistant');
          if (assistantMsg) {
            setLocalMessages(prev => [...prev, { role: 'assistant', text: assistantMsg.text || 'No response.' }]);
            setIsWaiting(false);
            setSessionMsgCount(messages.length);
            stopPolling();
          }
        }
      } catch (err) {
        console.error('Failed to poll chat session:', err);
      }
    }, 3000);
  }, [stopPolling]);

  // Reset chat ONLY when the actual node changes (not when chatSessionId updates)
  useEffect(() => {
    if (prevNodeIdRef.current !== node.id) {
      prevNodeIdRef.current = node.id;
      setChatInput('');
      setIsWaiting(false);
      setLocalMessages([]);
      setSessionMsgCount(null);
      stopPolling();
    }
  }, [node.id, stopPolling]);

  // Load existing messages when session becomes available
  // Does NOT reset if we're actively waiting for a response
  useEffect(() => {
    const sid = incident.chatSessionId;
    if (!sid) return;
    // Don't reload/reset if user is waiting for a response
    if (isWaitingRef.current) return;

    (async () => {
      try {
        const res = await fetch(`/api/chat-sessions/${sid}`);
        if (!res.ok) return;
        const data = await res.json();
        const messages = data.messages || [];
        setSessionMsgCount(messages.length);

        // Find inline chat messages (ones with [Re: ...] prefix)
        const inlineMessages: LocalMessage[] = [];
        for (const msg of messages) {
          if (msg.sender === 'user' && typeof msg.text === 'string' && msg.text.startsWith('[Re:')) {
            const cleanText = msg.text.replace(/^\[Re:.*?\]\s*/, '');
            inlineMessages.push({ role: 'user', text: cleanText });
          } else if (msg.sender === 'assistant' && inlineMessages.length > 0 && inlineMessages[inlineMessages.length - 1].role === 'user') {
            inlineMessages.push({ role: 'assistant', text: msg.text || 'No response.' });
          }
        }
        if (inlineMessages.length > 0) {
          setLocalMessages(inlineMessages);
        }

        // Resume polling if last inline message has no response
        if (inlineMessages.length > 0 && inlineMessages[inlineMessages.length - 1].role === 'user') {
          setIsWaiting(true);
          startPolling(sid, messages.length);
        }
      } catch (err) {
        console.error('Failed to load chat session:', err);
      }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.id, incident.chatSessionId]);

  const handleSend = async () => {
    const question = chatInput.trim();
    if (!question || isWaiting) return;

    const contextQuestion = `[Re: ${label} (${node.type})] ${question}`;
    setChatInput('');
    setLocalMessages(prev => [...prev, { role: 'user', text: question }]);
    setIsWaiting(true);

    try {
      // Use ref for session ID (survives prop changes)
      const sid = activeSessionIdRef.current || incident.chatSessionId;

      // Get current message count before sending
      let currentCount = sessionMsgCount;
      if (currentCount === null && sid) {
        try {
          const sessionRes = await fetch(`/api/chat-sessions/${sid}`);
          if (sessionRes.ok) {
            const sessionData = await sessionRes.json();
            currentCount = (sessionData.messages || []).length;
          }
        } catch {
          currentCount = 0;
        }
      }
      if (currentCount === null) currentCount = 0;
      setSessionMsgCount(currentCount);

      const chatUrl = sid
        ? `/api/incidents/${incident.id}/chat?session_id=${sid}`
        : `/api/incidents/${incident.id}/chat`;

      const response = await fetch(chatUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: contextQuestion, mode: 'agent' }),
      });

      if (!response.ok) throw new Error('Failed to send message');

      const data = await response.json();
      // Capture session ID from response so polling works even if prop hasn't updated yet
      const responseSid = data.session_id || sid;
      if (responseSid) activeSessionIdRef.current = responseSid;

      const newCount = (currentCount || 0) + 1;
      startPolling(responseSid || '', newCount);
    } catch (err) {
      console.error('Failed to send chat message:', err);
      setLocalMessages(prev => [...prev, { role: 'assistant', text: 'Failed to send message. Please try again.' }]);
      setIsWaiting(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const hasMatchedData = matchedAlerts.length > 0 || matchedCitations.length > 0 || matchedSuggestions.length > 0;

  return (
    <Panel
      position="top-left"
      className="bg-zinc-950/95 border border-zinc-700 rounded-lg shadow-2xl backdrop-blur-sm"
      style={{
        width: isFullscreen ? '340px' : '310px',
        maxHeight: isFullscreen ? '80vh' : '70vh',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-zinc-800 flex-shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          {Icon && <Icon size={16} style={{ color: colors.border, flexShrink: 0 }} />}
          <div className="min-w-0">
            <div className="text-sm font-semibold text-white truncate">{label}</div>
            <div className="text-[10px] text-zinc-500 uppercase tracking-wider font-bold">{node.type}</div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded text-zinc-500 hover:text-white hover:bg-zinc-800 transition-colors flex-shrink-0"
        >
          <X size={14} />
        </button>
      </div>

      {/* Status + Badges */}
      <div className="flex items-center gap-1.5 px-3 py-2 border-b border-zinc-800/50 flex-shrink-0">
        <span
          className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-semibold border"
          style={{ color: colors.border, borderColor: colors.border + '40', backgroundColor: colors.bg }}
        >
          <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: colors.border }} />
          {colors.label}
        </span>
        {node.isRootCause && (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-red-500/20 text-red-400 border border-red-500/30">
            Root Cause
          </span>
        )}
        {node.isAffected && (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-yellow-500/20 text-yellow-400 border border-yellow-500/30">
            Affected
          </span>
        )}
      </div>

      {/* Info Sections (scrollable) */}
      <div className="flex-1 overflow-y-auto min-h-0" style={{ maxHeight: localMessages.length > 0 ? '30%' : '60%' }}>
        {!hasMatchedData && (
          <div className="px-3 py-4 text-xs text-zinc-500 text-center">
            No direct evidence found for this resource in the investigation data.
          </div>
        )}

        {/* Matched Alerts */}
        {matchedAlerts.length > 0 && (
          <div className="px-3 py-2 border-b border-zinc-800/50">
            <div className="text-[10px] text-zinc-500 font-bold uppercase tracking-wider mb-1.5">
              Related Alerts ({matchedAlerts.length})
            </div>
            {matchedAlerts.slice(0, 3).map((alert, i) => (
              <div key={i} className="flex items-start gap-2 py-1">
                <AlertTriangle size={12} className="text-yellow-400 flex-shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <div className="text-xs text-zinc-300 truncate">{alert.alertTitle}</div>
                  <div className="text-[10px] text-zinc-500">{alert.alertService} - {alert.alertSeverity}</div>
                </div>
              </div>
            ))}
            {matchedAlerts.length > 3 && (
              <div className="text-[10px] text-zinc-500 mt-1">+{matchedAlerts.length - 3} more</div>
            )}
          </div>
        )}

        {/* Matched Citations */}
        {matchedCitations.length > 0 && (
          <div className="px-3 py-2 border-b border-zinc-800/50">
            <div className="text-[10px] text-zinc-500 font-bold uppercase tracking-wider mb-1.5">
              Evidence ({matchedCitations.length})
            </div>
            {matchedCitations.slice(0, 3).map((citation, i) => (
              <div key={i} className="flex items-start gap-2 py-1">
                <Terminal size={12} className="text-cyan-400 flex-shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <div className="text-[10px] text-zinc-400 font-mono truncate">{citation.toolName}</div>
                  <div className="text-[10px] text-zinc-500 truncate">{citation.command}</div>
                </div>
              </div>
            ))}
            {matchedCitations.length > 3 && (
              <div className="text-[10px] text-zinc-500 mt-1">+{matchedCitations.length - 3} more</div>
            )}
          </div>
        )}

        {/* Matched Suggestions */}
        {matchedSuggestions.length > 0 && (
          <div className="px-3 py-2 border-b border-zinc-800/50">
            <div className="text-[10px] text-zinc-500 font-bold uppercase tracking-wider mb-1.5">
              Suggestions ({matchedSuggestions.length})
            </div>
            {matchedSuggestions.slice(0, 3).map((suggestion, i) => {
              const typeIcon = suggestion.type === 'diagnostic' ? Terminal :
                suggestion.type === 'mitigation' ? Shield : MessageSquare;
              const TypeIcon = typeIcon;
              return (
                <div key={i} className="flex items-start gap-2 py-1">
                  <TypeIcon size={12} className="text-purple-400 flex-shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <div className="text-xs text-zinc-300 truncate">{suggestion.title}</div>
                    <div className="text-[10px] text-zinc-500 capitalize">{suggestion.type} - {suggestion.risk} risk</div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Inline Chat */}
      <div className="border-t border-zinc-800 flex-shrink-0 flex flex-col" style={{ minHeight: localMessages.length > 0 ? '40%' : 'auto' }}>
        {/* Messages */}
        {localMessages.length > 0 && (
          <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2 min-h-0" style={{ maxHeight: '200px' }}>
            {localMessages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div
                  className={`max-w-[90%] rounded-lg px-2.5 py-1.5 text-xs ${
                    msg.role === 'user'
                      ? 'bg-zinc-700 text-white'
                      : 'bg-zinc-900 text-zinc-300 border border-zinc-800'
                  }`}
                >
                  {msg.text}
                </div>
              </div>
            ))}
            {isWaiting && (
              <div className="flex justify-start">
                <div className="bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-xs text-zinc-400 flex items-center gap-1.5">
                  <Loader2 size={10} className="animate-spin" />
                  Investigating...
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}

        {/* Input */}
        <div className="flex items-center gap-1.5 px-2.5 py-2 border-t border-zinc-800/50">
          <input
            ref={inputRef}
            type="text"
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Ask about ${label}...`}
            disabled={isWaiting}
            className="flex-1 bg-zinc-900 border border-zinc-700 rounded-md px-2.5 py-1.5 text-xs text-white placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500 disabled:opacity-50 nodrag"
          />
          <button
            onClick={handleSend}
            disabled={!chatInput.trim() || isWaiting}
            className="p-1.5 rounded-md bg-zinc-700 hover:bg-zinc-600 text-white transition-colors disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0 nodrag"
          >
            <Send size={12} />
          </button>
        </div>
      </div>
    </Panel>
  );
}

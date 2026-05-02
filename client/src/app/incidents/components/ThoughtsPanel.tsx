'use client';

import { useState, useCallback, useEffect, useRef, type KeyboardEvent, type ChangeEvent } from 'react';
import { MessageSquare, Send } from 'lucide-react';
import { StreamingThought, Incident, ChatSession, incidentsService } from '@/lib/services/incidents';
import { MarkdownRenderer } from '@/components/ui/markdown-renderer';

// Maximum length for short titles in incident chat tabs
const TITLE_SHORT_MAX_LENGTH = 15;

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
}

interface ThoughtsPanelProps {
  thoughts: StreamingThought[];
  incident: Incident;
  isVisible: boolean;
  canInteract?: boolean;
}

/**
 * Extract the user message from a context-wrapped message.
 * The backend wraps user questions in <user_message>...</user_message> tags.
 */
function extractUserMessage(content: string): string {
  const match = content.match(/<user_message>\s*([\s\S]*?)\s*<\/user_message>/);
  if (match) {
    return match[1].trim();
  }
  return content;
}

/**
 * Generate a short title for a chat session from the user's question.
 * Uses the first 2-3 words, up to TITLE_SHORT_MAX_LENGTH characters.
 */
function generateShortTitle(question: string): string {
  const words = question.trim().split(/\s+/);
  
  // Take first 2-3 words, up to TITLE_SHORT_MAX_LENGTH characters
  let title = '';
  for (let i = 0; i < Math.min(3, words.length); i++) {
    const nextWord = words[i];
    if ((title + ' ' + nextWord).trim().length > TITLE_SHORT_MAX_LENGTH) break;
    title = (title + ' ' + nextWord).trim();
  }
  
  // If we got at least one word, use it; otherwise fallback to substring
  return title || question.substring(0, TITLE_SHORT_MAX_LENGTH);
}

/**
 * Strip the "Incident: " prefix from titles for display in tabs.
 * The prefix is kept in the database for chat history, but removed for tab display.
 */
function stripIncidentPrefix(title: string): string {
  return title.replace(/^Incident:\s*/i, '');
}

export default function ThoughtsPanel({ thoughts, incident, isVisible, canInteract = true }: ThoughtsPanelProps) {
  // 'thoughts' or session ID
  const [activeTab, setActiveTab] = useState<string>('thoughts');
  const [chatSessions, setChatSessions] = useState<ChatSession[]>(
    (incident.chatSessions || []).filter((s: ChatSession) => s.id !== incident.chatSessionId)
  );
  const [currentMessages, setCurrentMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [pollingSessionId, setPollingSessionId] = useState<string | null>(null);
  const pollStartRef = useRef<number>(0);

  const failSession = useCallback((sid: string) => {
    setChatSessions((prev: ChatSession[]) => prev.map((s: ChatSession) =>
      s.id === sid ? { ...s, status: 'failed' } : s
    ));
  }, []);
  
  // Track session IDs we're currently creating to avoid state conflicts with parent component.
  // When we send a message, we create an optimistic session in local state (chatSessions).
  // The parent component polls the backend every 3s and may not include the new session yet.
  // This Set tracks sessions that exist in the database (we have the ID) but haven't appeared
  // in incident.chatSessions (from parent's polled data) yet.
  const creatingSessionIds = useRef<Set<string>>(new Set());

  // Merge parent's incident.chatSessions (from polled backend data) with local state sessions.
  // Preserves optimistic sessions that exist in local state but not yet in parent's data.
  useEffect(() => {
    // Parent's data: from incident prop (polled from backend every 3s)
    // Exclude the RCA session — its content is already shown in the Thoughts tab
    const incidentSessions = (incident.chatSessions || []).filter(
      (s: ChatSession) => s.id !== incident.chatSessionId
    );
    
    // Clean up creatingSessionIds: remove IDs that now exist in parent's data
    // This prevents the Set from growing indefinitely
    incidentSessions.forEach((session: ChatSession) => {
      if (creatingSessionIds.current.has(session.id)) {
        creatingSessionIds.current.delete(session.id);
      }
    });
    
    setChatSessions((prevSessions: ChatSession[]) => {
      // Get IDs of sessions we're currently creating (optimistic, in local state)
      const creatingIds = creatingSessionIds.current;
      
      // Keep local sessions (from prevSessions, our component state) that are being created
      // but not yet in parent's polled data (incidentSessions)
      const localCreatingSessions = prevSessions.filter(
        (s: ChatSession) => creatingIds.has(s.id) && !incidentSessions.find((is: ChatSession) => is.id === s.id)
      );
      
      // Merge: parent's polled sessions + our local optimistic sessions
      const merged = [...incidentSessions, ...localCreatingSessions];
      return merged;
    });
  }, [incident.id, incident.chatSessions]);

  // Restore active tab only on initial mount or incident change (not during session creation)
  useEffect(() => {
    // Don't reset if we're creating a session
    if (creatingSessionIds.current.size > 0) return;
    
    const filteredSessions = (incident.chatSessions || []).filter(
      (s: ChatSession) => s.id !== incident.chatSessionId
    );
    if (incident.activeTab === 'chat' && filteredSessions.length > 0) {
      setActiveTab(filteredSessions[filteredSessions.length - 1].id);
    } else {
      setActiveTab('thoughts');
    }
  }, [incident.id]); // Only on incident ID change, not chatSessions

  // Cleanup: Clear creatingSessionIds on unmount to prevent memory leaks
  useEffect(() => {
    return () => {
      creatingSessionIds.current.clear();
    };
  }, []);

  // Load messages when switching to a chat session tab
  useEffect(() => {
    if (activeTab === 'thoughts') {
      setCurrentMessages([]);
      return;
    }

    const session = chatSessions.find((s: ChatSession) => s.id === activeTab);
    if (session) {
      // Convert session messages to ChatMessage format
      const messages: ChatMessage[] = (session.messages || []).map((m: any, idx: number) => {
        const sender = m.sender || m.role || m.type || 'assistant';
        const isUser = sender === 'user' || sender === 'human';
        let content = m.text || m.content || '';
        
        // For user messages, extract the actual question from context-wrapped messages
        if (isUser) {
          content = extractUserMessage(content);
        }
        
        return {
          id: `${session.id}-${idx}`,
          role: isUser ? 'user' : 'assistant',
          content,
        };
      }).filter((m: ChatMessage) => m.content.trim() !== '');
      
      // Merge server messages with local optimistic state.
      // The optimistic user message lives in local state while the streaming bot
      // response arrives from the server via polling.  We keep the local user
      // messages and append/update any server-side assistant messages.
      setCurrentMessages((prev: ChatMessage[]) => {
        if (pollingSessionId === activeTab && prev.length > 0) {
          const serverUserMessages = messages.filter((m: ChatMessage) => m.role === 'user');
          const localUserMessages = prev.filter((m: ChatMessage) => m.role === 'user');

          // Server has caught up with all optimistic user messages — use server state as-is
          if (serverUserMessages.length >= localUserMessages.length) {
            return messages;
          }

          // Check if assistant content actually changed since last render
          const serverAssistantMessages = messages.filter((m: ChatMessage) => m.role === 'assistant');
          if (serverAssistantMessages.length === 0) {
            return prev;
          }
          const prevAssistant = prev.filter((m: ChatMessage) => m.role === 'assistant');
          const prevAssistantLast = prevAssistant[prevAssistant.length - 1]?.content || '';
          const serverAssistantLast = serverAssistantMessages[serverAssistantMessages.length - 1]?.content || '';
          if (prevAssistant.length === serverAssistantMessages.length && prevAssistantLast === serverAssistantLast) {
            return prev;
          }

          // Server has new assistant content but hasn't persisted all our user messages yet.
          // Keep server order and append only the optimistic user messages the server is missing.
          // Re-key with "optimistic-" prefix to avoid React key collisions with server-indexed IDs.
          const optimisticTail = localUserMessages.slice(serverUserMessages.length).map((m) => ({
            ...m,
            id: `optimistic-${m.id}`,
          }));
          return [...messages, ...optimisticTail];
        }
        return messages;
      });

      // If session is in progress, start polling
      if (session.status === 'in_progress') {
        setPollingSessionId(session.id);
      }
    }
  }, [activeTab, chatSessions]);

  // Poll for session updates when a session is in progress
  useEffect(() => {
    if (!pollingSessionId) { pollStartRef.current = 0; return; }
    pollStartRef.current = Date.now();

    let isCancelled = false;
    const abortController = new AbortController();
    const sessionIdToFetch = pollingSessionId;

    const markSessionFailed = () => {
      setPollingSessionId(null);
      setIsLoading(false);
      failSession(sessionIdToFetch);
    };

    const pollInterval = setInterval(async () => {
      if (isCancelled) return;
      if (Date.now() - pollStartRef.current > 5 * 60 * 1000) { markSessionFailed(); return; }
      
      try {
        const sessionResp = await fetch(`/api/chat-sessions/${sessionIdToFetch}`, {
          signal: abortController.signal,
        });
        if (!sessionResp.ok || isCancelled) return;

        const sessionData = await sessionResp.json();
        if (isCancelled) return;

        setChatSessions((prev: ChatSession[]) => prev.map((s: ChatSession) => 
          s.id === sessionIdToFetch 
            ? { ...s, messages: sessionData.messages || [], status: sessionData.status }
            : s
        ));
        pollStartRef.current = Date.now();

        // If completed or failed, stop polling and remove from creating set
        if (sessionData.status === 'completed' || sessionData.status === 'failed') {
          setPollingSessionId(null);
          setIsLoading(false);
          creatingSessionIds.current.delete(sessionIdToFetch);
        }
      } catch (error) {
        // Only log real errors (not expected aborts or cancelled requests)
        if (!isCancelled && !(error instanceof Error && error.name === 'AbortError')) {
          console.error('Error polling session:', error);
        }
      }
    }, 2000);

    return () => {
      isCancelled = true;
      abortController.abort();
      clearInterval(pollInterval);
    };
  }, [pollingSessionId]);

  // Handler to update active tab and persist to backend
  const handleTabChange = useCallback((tabId: string) => {
    setActiveTab(tabId);
    const isChat = tabId !== 'thoughts';
    incidentsService.updateActiveTab(incident.id, isChat ? 'chat' : 'thoughts');
    
    // Update loading state based on the actual session status (not just pollingSessionId)
    // This handles the case where user switches back after session completed while away
    if (tabId === 'thoughts') {
      setIsLoading(false);
    } else {
      const session = chatSessions.find((s: ChatSession) => s.id === tabId);
      setIsLoading(session?.status === 'in_progress');
    }
  }, [incident.id, chatSessions]);

  const handleSend = useCallback(async () => {
    if (!inputValue.trim() || isLoading) return;

    const question = inputValue.trim();
    setInputValue('');
    setIsLoading(true);

    // Check if we're continuing an existing session (in a chat tab) or creating a new one
    const isExistingSession = activeTab !== 'thoughts';
    const sessionIdToUse = isExistingSession ? activeTab : undefined;

    try {
      // Build the URL with session_id query param if continuing an existing session
      const chatUrl = sessionIdToUse 
        ? `/api/incidents/${incident.id}/chat?session_id=${sessionIdToUse}`
        : `/api/incidents/${incident.id}/chat`;

      const response = await fetch(chatUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to get response');
      }

      const sessionId = data.session_id;
      const isNewSession = data.is_new_session !== false; // Default to true if not specified

      if (isNewSession) {
        // Track that we're creating this session (exists in DB but not in parent's polled data yet)
        creatingSessionIds.current.add(sessionId);

        // Create the optimistic user message (shown immediately in local state)
        const userMessage: ChatMessage = {
          id: `${sessionId}-0`,
          role: 'user',
          content: question,
        };

        // Create a new chat session entry in local state (optimistic - before parent's poll includes it)
        const newSession: ChatSession = {
          id: sessionId,
          title: generateShortTitle(question),
          messages: [{ text: question, sender: 'user' }],
          status: 'in_progress',
          createdAt: new Date().toISOString(),
        };

        // Set everything in the right order: messages first, then session, then tab
        // All of these update local component state (not parent's data)
        setCurrentMessages([userMessage]);
        setChatSessions((prev: ChatSession[]) => [...prev, newSession]);
        setActiveTab(sessionId);
        setPollingSessionId(sessionId);
      } else {
        // Continuing existing session - add optimistic user message to current messages
        const userMessage: ChatMessage = {
          id: `${sessionId}-${Date.now()}`,
          role: 'user',
          content: question,
        };

        setCurrentMessages((prev: ChatMessage[]) => [...prev, userMessage]);
        
        // Update session status to in_progress in local state
        setChatSessions((prev: ChatSession[]) => prev.map((s: ChatSession) => 
          s.id === sessionId 
            ? { ...s, status: 'in_progress', messages: [...(s.messages || []), { text: question, sender: 'user' }] }
            : s
        ));
        
        setPollingSessionId(sessionId);
      }

    } catch (error) {
      setCurrentMessages((prev: ChatMessage[]) => [...prev, {
        id: `msg-${Date.now()}-error`,
        role: 'assistant',
        content: `Sorry, I couldn't process your question. ${error instanceof Error ? error.message : 'Please try again.'}`,
      }]);
      setIsLoading(false);
    }
  }, [inputValue, isLoading, incident.id, activeTab]);

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Don't render RCA panel for merged incidents or when panel is hidden
  if (incident.status === 'merged') return null;
  if (!isVisible) return null;

  return (
    <div className="fixed top-[49px] right-0 h-[calc(100vh-49px)] w-[400px] bg-background z-20 border-l border-zinc-800/50 flex flex-col">
      {/* Tab Bar */}
      <div className="flex items-center border-b border-zinc-800/50 bg-zinc-900/50 px-2 h-10 shrink-0 overflow-x-auto">
        {/* Thoughts tab */}
        <button
          onClick={() => handleTabChange('thoughts')}
          className={`px-3 py-1.5 text-sm rounded-t-md transition-colors whitespace-nowrap ${
            activeTab === 'thoughts' ? 'bg-background text-white border-b-2 border-orange-500' : 'text-zinc-400 hover:text-zinc-200'
          }`}
        >
          Thoughts
          {(incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing') && <span className="ml-1.5 w-2 h-2 bg-orange-400 rounded-full animate-pulse inline-block" />}
        </button>

        {/* Chat session tabs */}
        {chatSessions.map((session: ChatSession) => (
          <button
            key={session.id}
            onClick={() => handleTabChange(session.id)}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-t-md transition-colors whitespace-nowrap ${
              activeTab === session.id ? 'bg-background text-white border-b-2 border-orange-500' : 'text-zinc-400 hover:text-zinc-200'
            }`}
          >
            <MessageSquare className="w-3.5 h-3.5" />
            {stripIncidentPrefix(session.title)}
            {session.status === 'in_progress' && <span className="ml-1 w-2 h-2 bg-orange-400 rounded-full animate-pulse inline-block" />}
          </button>
        ))}
      </div>

      {/* Main Thoughts View */}
      {activeTab === 'thoughts' && (
        <div className="flex-1 relative overflow-hidden">
          <div className="absolute inset-0 overflow-y-auto p-5 pb-32">
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
              {thoughts.length === 0 && incident.auroraStatus !== 'running' && incident.auroraStatus !== 'summarizing' && (
                <p className="text-center text-zinc-500 text-sm py-8">No investigation thoughts yet</p>
              )}
            </div>
          </div>

          {/* Input at bottom */}
          <div className="absolute bottom-0 left-0 right-0">
            <div className="h-4 bg-gradient-to-t from-background to-transparent" />
            <div className="px-4 pb-4 bg-background">
              {canInteract ? (
                <div className="relative">
                  <input
                    type="text"
                    value={inputValue}
                    onChange={(e: ChangeEvent<HTMLInputElement>) => setInputValue(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="Ask about this investigation..."
                    className="w-full bg-zinc-800 border-0 rounded-md pl-3 pr-10 py-2 text-sm text-zinc-300 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-700 transition-colors"
                    disabled={isLoading}
                  />
                  <button
                    onClick={handleSend}
                    disabled={!inputValue.trim() || isLoading}
                    className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-zinc-500 hover:text-zinc-300 disabled:text-zinc-700 transition-colors"
                  >
                    <Send className="w-3.5 h-3.5" />
                  </button>
                </div>
              ) : (
                <p className="text-xs text-zinc-500 text-center py-2">Read-only access. Editors and admins can interact with investigations.</p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Chat View - for any chat session tab */}
      {activeTab !== 'thoughts' && (
        <div className="flex-1 relative overflow-hidden">
          <div className="absolute inset-0 overflow-y-auto p-5 pb-32">
            <div className="space-y-4">
              {currentMessages.map((msg: ChatMessage) => (
                <div key={msg.id} className={
                  msg.role === 'user'
                    ? 'pl-4 border-l-2 border-blue-500/50'
                    : 'pl-4 border-l-2 border-zinc-700 hover:border-orange-500/50 transition-colors'
                }>
                  <div className="text-xs text-zinc-500 mb-1">
                    {msg.role === 'user' ? 'You' : 'Aurora'}
                  </div>
                  <div className="text-sm text-zinc-300 break-words leading-relaxed min-w-0 overflow-hidden">
                    <MarkdownRenderer content={msg.content} />
                  </div>
                </div>
              ))}
              {isLoading && pollingSessionId === activeTab && (
                <div className="pl-4 border-l-2 border-orange-500/50">
                  <div className="flex items-center gap-2 text-sm text-zinc-400">
                    <div className="flex gap-1">
                      <span className="w-1.5 h-1.5 bg-orange-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                      <span className="w-1.5 h-1.5 bg-orange-400 rounded-full animate-bounce" style={{ animationDelay: '100ms' }} />
                      <span className="w-1.5 h-1.5 bg-orange-400 rounded-full animate-bounce" style={{ animationDelay: '200ms' }} />
                    </div>
                    <span>Thinking...</span>
                  </div>
                </div>
              )}
              {currentMessages.length === 0 && !isLoading && (
                <p className="text-center text-zinc-500 text-sm py-8">No messages in this chat yet</p>
              )}
            </div>
          </div>

          {/* Input at bottom */}
          <div className="absolute bottom-0 left-0 right-0">
            <div className="h-4 bg-gradient-to-t from-background to-transparent" />
            <div className="px-4 pb-4 bg-background">
              {canInteract ? (
                <div className="relative">
                  <input
                    type="text"
                    value={inputValue}
                    onChange={(e: ChangeEvent<HTMLInputElement>) => setInputValue(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="Ask a follow-up..."
                    className="w-full bg-zinc-800 border-0 rounded-md pl-3 pr-10 py-2 text-sm text-zinc-300 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-700 transition-colors"
                    disabled={isLoading}
                  />
                  <button
                    onClick={handleSend}
                    disabled={!inputValue.trim() || isLoading}
                    className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-zinc-500 hover:text-zinc-300 disabled:text-zinc-700 transition-colors"
                  >
                    <Send className="w-3.5 h-3.5" />
                  </button>
                </div>
              ) : (
                <p className="text-xs text-zinc-500 text-center py-2">Read-only access. Editors and admins can interact with investigations.</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

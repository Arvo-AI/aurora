"use client"

import React, { useState, useContext, useEffect, useMemo, useRef } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Trash2, MoreVertical, Edit3, Loader2 } from 'lucide-react';
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { useChatHistory, type ChatSession } from '@/hooks/useChatHistory';
import { useChatExpansion } from '@/app/components/ClientShell';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogTrigger,
  AlertDialogContent,
  AlertDialogHeader,
  AlertDialogFooter,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogAction,
  AlertDialogCancel,
} from "@/components/ui/alert-dialog";

interface ChatHistoryProps {
  onSessionSelect: (sessionId: string) => void;
  onNewChat: () => void;
  currentSessionId?: string | null;
  className?: string;
}

export default function ChatHistory({ 
  onSessionSelect, 
  onNewChat, 
  currentSessionId,
  className,
}: ChatHistoryProps) {

  const { setRefreshChatHistory } = useChatExpansion();
  const { 
    sessions, 
    loading, 
    error, 
    deleteSession, 
    updateSession,
    refreshSessions,
    deleteAllSessions,
    cleanupPhantomSessions
  } = useChatHistory();


  const pathname = usePathname();
  const router = useRouter();

  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState<string>('');
  const [isClearingAll, setIsClearingAll] = useState<boolean>(false);
  const [showClearDialog, setShowClearDialog] = useState<boolean>(false);
  const [isDarkMode, setIsDarkMode] = useState<boolean>(false);

  // Register refresh function with context on mount (only once)
  const refreshSessionsRef = useRef(refreshSessions);
  refreshSessionsRef.current = refreshSessions;
  
  useEffect(() => {
    setRefreshChatHistory(() => () => refreshSessionsRef.current());
  }, [setRefreshChatHistory]); // Only run once, use ref for latest function

  // Auto-cleanup phantom sessions on mount (only once)
  const cleanupPhantomSessionsRef = useRef(cleanupPhantomSessions);
  cleanupPhantomSessionsRef.current = cleanupPhantomSessions;
  
  useEffect(() => {
    const performCleanup = async () => {
      try {
        await cleanupPhantomSessionsRef.current();
      } catch (err) {
        console.debug('Cleanup failed:', err);
      }
    };

    // Cleanup on component mount after a short delay
    const timer = setTimeout(performCleanup, 1000);
    return () => clearTimeout(timer);
  }, []); // Only run once on mount

  // Track dark mode changes
  useEffect(() => {
    const checkDarkMode = () => {
      setIsDarkMode(document.documentElement.classList.contains('dark'));
    };

    // Initial check
    checkDarkMode();

    // Watch for changes
    const observer = new MutationObserver(checkDarkMode);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class']
    });

    return () => observer.disconnect();
  }, []);

  const handleClearAllChats = async () => {
    if (isClearingAll) return;
    setIsClearingAll(true);
    try {
      console.log(`[ChatHistory] Current session ID: ${currentSessionId}`);
      console.log(`[ChatHistory] Available sessions:`, sessions.map(s => ({ id: s.id, title: s.title })));
            
      // Always pass the current session ID to preserve it
      const success = await deleteAllSessions(currentSessionId ?? null);
      if (success) {
        console.log(`Cleared all chat sessions except current session: ${currentSessionId}`);
      }
      setShowClearDialog(false);
    } catch (err) {
      console.error('Failed to clear all chats:', err);
    } finally {
      setIsClearingAll(false);
    }
  };

  const handleSessionClick = (sessionId: string) => {
    // Don't navigate if we're in rename mode
    if (renamingId === sessionId) return;

    // Find the session object to get the title
    const session = sessions.find(s => s.id === sessionId);

    // Update URL to reflect the selected session so that page refresh
    // returns to the same conversation. Use a shallow replace to avoid a
    // full navigation.
    if (pathname === '/chat') {
      router.replace(`/chat?sessionId=${sessionId}`);
    } else {
      router.push(`/chat?sessionId=${sessionId}`);
    }

    onSessionSelect(sessionId);
  };

  const handleRenameSession = async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    
    if (renamingId === sessionId) {
      // Save the rename
      if (newTitle.trim()) {
        try {
          const success = await updateSession(sessionId, newTitle.trim());
          if (success) {
            await refreshSessions();
          }
        } catch (err) {
          console.error('Failed to rename session:', err);
          alert('Failed to rename chat. Please try again.');
        }
      }
      setRenamingId(null);
      setNewTitle('');
    } else {
      // Start renaming
      const session = sessions.find(s => s.id === sessionId);
      if (session) {
        setRenamingId(sessionId);
        setNewTitle(session.title);
      }
    }
  };

  const handleDeleteSession = async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation(); // Prevent session selection
    
    if (deletingId === sessionId) return; // Prevent double-click
    
    setDeletingId(sessionId);
    try {
      const success = await deleteSession(sessionId);
      if (success) {
        // If we deleted the current session, trigger new chat
        if (currentSessionId === sessionId) {
          onNewChat();
        }
      }
    } catch (err) {
      console.error('Failed to delete session:', err);
    } finally {
      setDeletingId(null);
    }
  };

  const truncateTitle = (title: string, maxLength: number = 30) => {
    if (title.length <= maxLength) return title;
    return title.substring(0, maxLength).trim() + '...';
  };

  // Dynamic scrollbar colors for Firefox
  const scrollbarStyles = useMemo(() => ({
    scrollbarWidth: 'thin' as const,
    scrollbarColor: isDarkMode 
      ? 'rgb(75 85 99) rgb(31 41 55)' // Dark mode: thumb track
      : 'rgb(156 163 175) rgb(243 244 246)' // Light mode: thumb track
  }), [isDarkMode]);

  if (error) {
    return (
      <div className={cn("p-2", className)}>
        <div className="text-sm text-destructive">
          Failed to load chat history
        </div>
        <Button 
          variant="ghost" 
          size="sm" 
          onClick={refreshSessions}
          className="mt-1 text-xs"
        >
          Retry
        </Button>
      </div>
    );
  }

  return (
    <>
      <style jsx>{`
        .chat-history-scroll::-webkit-scrollbar {
          width: 6px;
        }
        .chat-history-scroll::-webkit-scrollbar-track {
          background: rgb(243 244 246);
        }
        .chat-history-scroll::-webkit-scrollbar-thumb {
          background: rgb(156 163 175);
          border-radius: 3px;
        }
        .chat-history-scroll::-webkit-scrollbar-thumb:hover {
          background: rgb(107 114 128);
        }
        :global(.dark) .chat-history-scroll::-webkit-scrollbar-track {
          background: rgb(31 41 55);
        }
        :global(.dark) .chat-history-scroll::-webkit-scrollbar-thumb {
          background: rgb(75 85 99);
        }
        :global(.dark) .chat-history-scroll::-webkit-scrollbar-thumb:hover {
          background: rgb(107 114 128);
        }
      `}</style>
      <div className={cn("flex flex-col h-full", className)}>
        <div className="flex justify-between items-center mb-2 px-2 flex-shrink-0">
        <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          Chat History
        </div>
        <div className="flex items-center gap-1">
            {/* Clear all */}
            <AlertDialog open={showClearDialog} onOpenChange={setShowClearDialog}>
              <AlertDialogTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-xs h-5 px-1.5 hover:bg-muted text-muted-foreground"
                  disabled={isClearingAll || sessions.length === 0}
                  title="Clear all chat history"
                >
                  {isClearingAll ? "Clearing..." : "Clear"}
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Clear all chat history?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This will permanently delete <b>all</b> your chat sessions. This action cannot be undone.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel disabled={isClearingAll}>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={handleClearAllChats}
                    disabled={isClearingAll}
                    className="bg-destructive text-white hover:bg-destructive/90"
                  >
                    {isClearingAll ? "Clearing..." : "Clear"}
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
        </div>
      </div>

      {/* Sessions List */}
      <div 
        className="flex-1 overflow-y-scroll min-h-0 px-1 mb-4 chat-history-scroll" 
        style={scrollbarStyles}
      >
        {loading ? (
          <div className="space-y-1">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-center justify-between px-2.5 py-1.5 rounded-md">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-4 w-4 rounded-full" />
              </div>
            ))}
          </div>
        ) : sessions.length === 0 ? (
          <div className="p-2 text-sm text-muted-foreground">No chats yet</div>
        ) : (
          <div className="space-y-1">
            {[
              ...sessions.filter(session => session.title === "New Chat"),
              ...sessions.filter(session => session.title !== "New Chat")
            ].map((session) => {
              const isRenaming = renamingId === session.id;
              const isInProgress = session.status === 'in_progress';

              const RowContent = (
                <>
                  {/* Loading spinner for in_progress sessions */}
                  {isInProgress && (
                    <Loader2 size={14} className="animate-spin text-muted-foreground mr-2 flex-shrink-0" />
                  )}
                  <div className="min-w-0 flex-1">
                    {isRenaming ? (
                      <input
                        type="text"
                        value={newTitle}
                        onChange={(e) => setNewTitle(e.target.value)}
                        onBlur={() =>
                          handleRenameSession(
                            session.id,
                            { stopPropagation: () => {} } as any
                          )
                        }
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            handleRenameSession(session.id, {
                              stopPropagation: () => {},
                            } as any);
                          } else if (e.key === "Escape") {
                            setRenamingId(null);
                            setNewTitle("");
                          }
                        }}
                        autoFocus
                        className="w-full bg-transparent border-none outline-none text-sm p-0 m-0"
                        onClick={(e) => e.stopPropagation()}
                      />
                    ) : (
                      <div className="truncate text-sm" title={session.title}>
                        {truncateTitle(session.title)}
                      </div>
                    )}
                  </div>

                  {/* Session Options - hide for in_progress sessions */}
                  {!isInProgress && (
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-5 w-5 p-0 opacity-30 group-hover:opacity-100 hover:bg-muted ml-1 transition-opacity duration-200"
                          onClick={(e) => e.stopPropagation()}
                          title="Chat options"
                        >
                          <MoreVertical size={10} />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-36">
                        <DropdownMenuItem
                          onClick={(e) => handleRenameSession(session.id, e)}
                          className="text-foreground focus:text-foreground"
                        >
                          <Edit3 size={12} className="mr-2" />
                          Rename
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          onClick={(e) => handleDeleteSession(session.id, e)}
                          disabled={deletingId === session.id}
                          className="text-destructive focus:text-destructive"
                        >
                          <Trash2 size={12} className="mr-2" />
                          {deletingId === session.id ? "Deleting..." : "Delete"}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  )}
                </>
              );

              return (
                <div
                  key={session.id}
                  className={cn(
                    "group w-full flex items-center justify-between px-2.5 py-1.5 rounded-md transition-colors text-left text-sm",
                    isInProgress
                      ? "opacity-60 cursor-not-allowed"
                      : "cursor-pointer",
                    currentSessionId === session.id
                      ? "bg-card text-foreground border border-border shadow-sm"
                      : "text-muted-foreground hover:bg-primary/10 hover:text-foreground"
                  )}
                  onClick={() => !isInProgress && handleSessionClick(session.id)}
                  title={isInProgress ? "Analysis in progress..." : undefined}
                >
                  {RowContent}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
    </>
  );
} 
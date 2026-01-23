"use client"

import Link from "next/link"
import { usePathname, useRouter } from "next/navigation"
import { ChevronLeft, List, Settings, Edit3, LogOut, User, HelpCircle, Bug, Sparkles, Plug, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import ChatHistory from "@/components/ChatHistory"
import { useState, useEffect, useRef } from "react"
import { useUser } from "@/hooks/useAuthHooks"
import { signOut } from "next-auth/react"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import AuroraCreditProgressBar from "@/components/AuroraCreditProgressBar"

interface NavigationProps {
  isChatExpanded?: boolean;
  onChatExpandToggle?: () => void;
  isExpanded: boolean;
  setIsExpanded: (value: boolean) => void;
  isCodeSectionExpanded?: boolean;
  setIsCodeSectionExpanded?: (value: boolean) => void;
  showCodeSection?: boolean;
  onChatSessionSelect?: (sessionId: string) => void;
  onNewChat?: () => void;
  currentChatSessionId?: string | null;
  onSettingsClick?: () => void;
}

export default function Navigation({ 
  isChatExpanded,
  onChatExpandToggle,
  isExpanded,
  setIsExpanded,
  isCodeSectionExpanded,
  setIsCodeSectionExpanded,
  showCodeSection,
  onChatSessionSelect,
  onNewChat,
  currentChatSessionId,
  onSettingsClick,
}: NavigationProps) {
  const pathname = usePathname()
  const router = useRouter()
  const { user } = useUser()
  const [isUserMenuOpen, setIsUserMenuOpen] = useState(false)
  const userMenuRef = useRef<HTMLDivElement>(null)


  const toggleNavigation = () => {
    setIsExpanded(!isExpanded)
  }

  // Handle clicks outside the user menu to close it
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setIsUserMenuOpen(false)
      }
    }

    if (isUserMenuOpen) {
      document.addEventListener('mousedown', handleClickOutside)
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [isUserMenuOpen])





  // Custom sidebar icon component to match the provided image
  const SidebarIcon = () => (
    <svg 
      width="20" 
      height="20" 
      viewBox="0 0 24 24" 
      fill="none" 
      stroke="currentColor" 
      strokeWidth="1.5" 
      strokeLinecap="round" 
      strokeLinejoin="round"
    >
      <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
      <line x1="9" y1="3" x2="9" y2="21" />
    </svg>
  )

  // Default handler: if parent did not provide a session selector, navigate to /chat and pass sessionId as query param
  const handleSessionSelectFallback = (sessionId: string) => {
    // If we're already on /chat, just navigate without query params so Deploy page can handle internally
    if (pathname === "/chat") {
      router.push("/chat");
    } else {
      router.push(`/chat?sessionId=${sessionId}`);
    }
  };

  return (
    <div className="h-full flex-shrink-0 relative">
      {/* Navigation sidebar */}
      <nav className={cn(
        "bg-muted dark:bg-[#111111] h-full border-r border-border transition-all duration-300 overflow-hidden flex flex-col",
        isExpanded ? "w-56" : "w-0"
      )}>
        <div className="p-3 flex items-center justify-between border-b border-border/30">
          <div className="flex items-center gap-2">
            <img 
              src="/arvologotransparent-modified.png" 
              alt="Arvo Logo" 
              className="w-10 h-10 block dark:hidden"
            />
            <img 
              src="/arvologotransparent.png" 
              alt="Arvo Logo" 
              className="w-10 h-10 hidden dark:block"
            />
            <div className="flex flex-col items-start">
              <h1 className="text-lg font-bold text-foreground">Aurora</h1>
              <span className="px-1.5 py-0.5 text-xs font-semibold tracking-wider text-blue-700 bg-blue-100 rounded-full dark:bg-blue-900 dark:text-blue-200 mt-0.5">
                BETA
              </span>
            </div>
          </div>
          <Button 
            variant="ghost" 
            size="sm" 
            className="h-7 w-7 p-0"
            onClick={toggleNavigation}
          >
            <ChevronLeft size={14} />
          </Button>
        </div>

        {/* Menu content */}
        <ul className="flex flex-col p-2.5 space-y-1 flex-1 min-h-0 overflow-hidden">
          {/* Incidents - with running indicator */}
          <li>
            <Link
              href="/incidents"
              className={cn(
                "w-full flex items-center justify-between px-2.5 py-1.5 rounded-md hover:bg-primary/10 transition-colors text-sm border border-transparent hover:border-border/50",
                pathname?.startsWith("/incidents")
                  ? "bg-card rounded-lg border border-border shadow-sm" 
                    : "text-muted-foreground"
                )}
              >
                <div className="flex items-center">
                  <Zap size={16} className="text-orange-400" />
                  <span className="ml-2">Incidents</span>
                </div>
                {/* Running indicator - shows when Aurora is investigating */}
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-orange-400 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-orange-500"></span>
                </span>
              </Link>
            </li>

          {/* Connectors Navigation Item */}
          <li>
            <Link
              href="/connectors"
              className={cn(
                "w-full flex items-center justify-between px-2.5 py-1.5 rounded-md hover:bg-primary/10 transition-colors text-sm border border-transparent hover:border-border/50",
                pathname === "/connectors" 
                  ? "bg-card rounded-lg border border-border shadow-sm" 
                  : "text-muted-foreground"
              )}
            >
              <div className="flex items-center">
                <Plug size={16} />
                <span className="ml-2">Connectors</span>
              </div>
            </Link>
          </li>

          {/* New Chat Button - accessible from all pages */}
          <li>
            <div
              onClick={() => {
                if (pathname === "/chat") {
                  // If already on chat page, just add the query parameter to trigger new chat
                  window.location.href = "/chat?newChat=true";
                } else {
                  // Otherwise, navigate to chat page for new chat
                  // Add a parameter to indicate this is a new chat request
                  router.push("/chat?newChat=true");
                }
              }}
              className={cn(
                "w-full flex items-center justify-between px-2.5 py-1.5 rounded-md hover:bg-primary/10 transition-colors text-sm border border-transparent hover:border-border/50 cursor-pointer",
                pathname === "/chat" 
                  ? "bg-card rounded-lg border border-border shadow-sm" 
                  : "text-muted-foreground"
              )}
            >
              <div className="flex items-center">
                <Edit3 size={16} />
                <span className="ml-2">New Chat</span>
              </div>

            </div>
          </li>

          {/* Chat History Section */}
          {(
            <li className="flex-1 min-h-0 overflow-hidden">
              <div className="border-t border-border/30 mt-2 pt-2 flex flex-col h-full min-h-0 overflow-hidden">
                <ChatHistory
                  onSessionSelect={onChatSessionSelect || handleSessionSelectFallback}
                  onNewChat={onNewChat || (() => {})}
                  currentSessionId={currentChatSessionId}
                  className="flex-1 min-h-0"
                />
              </div>
            </li>
          )}
        </ul>
        
        {/* User Section at Bottom */}
        <div className="border-t border-border/30 p-3">
          <div className="flex justify-center">
            <div className="w-full">
              {user ? (
                <div className="space-y-2">
                  {/* API Cost Tracking for signed-in users */}
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <div>
                          <AuroraCreditProgressBar />
                        </div>
                      </TooltipTrigger>
                      <TooltipContent side="right" className="max-w-sm">
                        <div className="text-sm space-y-1">
                          <p className="font-medium">API Cost Tracking</p>
                          <p className="text-muted-foreground">
                            Track your LLM API usage costs.
                          </p>
                        </div>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  
                  <div className="relative" ref={userMenuRef}>
                    <div 
                      className="flex items-center gap-3 w-full px-3 py-2 rounded-md hover:bg-primary/10 transition-colors text-sm border border-transparent hover:border-border/50 cursor-pointer"
                      onClick={() => setIsUserMenuOpen(!isUserMenuOpen)}
                    >
                    <Avatar className="w-8 h-8">
                      <AvatarImage src={user.imageUrl} alt={user.fullName || "User"} />
                      <AvatarFallback className="bg-primary text-primary-foreground font-medium">
                        {(user.fullName || user.firstName || user.emailAddresses[0]?.emailAddress || "U")
                          .split(" ")
                          .map(n => n[0])
                          .join("")
                          .toUpperCase()
                          .slice(0, 2)}
                      </AvatarFallback>
                    </Avatar>
                    <div className="flex flex-col flex-1 min-w-0">
                      <p className="text-sm font-medium text-foreground truncate">
                        {user.fullName || user.firstName || user.emailAddresses[0]?.emailAddress}
                      </p>
                      {user.emailAddresses[0]?.emailAddress && user.fullName && (
                        <p className="text-xs text-muted-foreground truncate">
                          {user.emailAddresses[0].emailAddress}
                        </p>
                      )}
                    </div>
                  </div>
                  
                  {isUserMenuOpen && (
                    <div className="absolute bottom-full left-0 right-0 mb-1 bg-card border border-border rounded-md shadow-lg z-50 min-w-[200px]">
                      <div className="p-2">
                        {/* User Email */}
                        <div className="px-2 py-1 mb-2">
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <p className="text-xs text-muted-foreground truncate">
                                  {user.emailAddresses[0]?.emailAddress}
                                </p>
                              </TooltipTrigger>
                              <TooltipContent>
                                <p>{user.emailAddresses[0]?.emailAddress}</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        </div>
                        
                        {/* Settings Button */}
                        <button
                          onClick={() => {
                            onSettingsClick?.();
                            setIsUserMenuOpen(false);
                          }}
                          className="flex items-center gap-2 w-full px-2 py-2 text-sm text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors"
                        >
                          <Settings className="h-4 w-4" />
                          Settings
                        </button>
                        
                        {/* Sign Out Button */}
                        <button
                          onClick={() => {
                            signOut();
                            setIsUserMenuOpen(false);
                          }}
                          className="flex items-center gap-2 w-full px-2 py-2 text-sm text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors"
                        >
                          <LogOut className="h-4 w-4" />
                          Sign out
                        </button>
                      </div>
                    </div>
                  )}
                  </div>
                </div>
              ) : (
                /* Sign In Button for non-authenticated users */
                <div className="space-y-2">
                  <Link href="/sign-in">
                    <button className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-md bg-primary hover:bg-primary/90 transition-colors text-sm text-primary-foreground font-medium">
                      <User className="h-4 w-4" />
                      Sign in
                    </button>
                  </Link>
                </div>
              )}
            </div>
          </div>
        </div>
      </nav>
      
      {/* Custom sidebar toggle button - always visible */}
      {!isExpanded && (
        <>
        <Button 
          variant="outline" 
          size="sm" 
          className="absolute top-16 left-4 h-10 w-10 rounded-full p-0 shadow-md border border-border bg-card z-50"
          onClick={toggleNavigation}
          style={{ position: 'absolute', top: '64px', left: '16px' }}
        >
          <SidebarIcon />
        </Button>
      </>
      )}
    </div>
  )
}

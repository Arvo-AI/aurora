"use client";

import React, { useState, useRef, useEffect, useCallback } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useUser } from "@/hooks/useAuthHooks";
import { signOut } from "next-auth/react";
import Link from "next/link";
import dynamic from "next/dynamic";
import { Button } from "@/components/ui/button";
import { Settings, LogOut } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useChatExpansion } from "./ClientShell";
import Navigation from "@/components/navigation";
import { IaCWorkspace } from "@/components/iac/IaCWorkspace";

// Dynamic imports for heavy components
const SettingsModal = dynamic(() => import("@/components/SettingsModal").then(mod => ({ default: mod.SettingsModal })), {
  ssr: false
});

// Custom sidebar icon component
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
);

// SidebarStrip component (moved from layout.tsx)
const SidebarStrip = ({ 
  isNavExpanded, 
  onSidebarToggle, 
  onNewChatClick,
  isSettingsModalOpen,
  setIsSettingsModalOpen
}: { 
  isNavExpanded: boolean; 
  onSidebarToggle: () => void; 
  onNewChatClick: () => void; 
  isSettingsModalOpen: boolean;
  setIsSettingsModalOpen: (open: boolean) => void;
}) => {
  const pathname = usePathname();
  const { user } = useUser();
  const [isUserMenuOpen, setIsUserMenuOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);

  // Handle clicks outside the user menu to close it
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setIsUserMenuOpen(false);
      }
    };

    if (isUserMenuOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [isUserMenuOpen]);

  if (isNavExpanded) return null;

  return (
    <TooltipProvider>
      <div className="absolute left-0 top-0 h-full w-16 bg-card border-r border-border flex flex-col items-center justify-between py-4 z-50">
        <div className="flex flex-col items-center space-y-4">
          {/* Sidebar toggle button */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button 
                variant="outline" 
                size="sm" 
                className="h-10 w-10 rounded-full p-0 shadow-md border border-border bg-card flex items-center justify-center"
                onClick={onSidebarToggle}
              >
                <SidebarIcon />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right" sideOffset={5}>
              <p>Open Sidebar</p>
            </TooltipContent>
          </Tooltip>
        </div>
        
        {/* User button at bottom */}
        {user && (
          <div className="relative mt-auto" ref={userMenuRef}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-10 w-10 rounded-full p-0 shadow-md border border-border bg-card flex items-center justify-center overflow-hidden"
                  onClick={() => setIsUserMenuOpen(!isUserMenuOpen)}
                >
                  <img 
                    src={user.imageUrl} 
                    alt={user.fullName || "User"} 
                    className="w-full h-full object-cover"
                  />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                <p>User Menu</p>
              </TooltipContent>
            </Tooltip>
            
            {isUserMenuOpen && (
              <div className="absolute bottom-full left-0 right-0 mb-1 bg-card border border-border rounded-md shadow-lg z-50 min-w-[200px]">
                <div className="p-2">
                  {/* User Email */}
                  <div className="px-2 py-1 mb-2">
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
                  </div>
                  
                  {/* Settings Button */}
                  <button
                    onClick={() => {
                      setIsSettingsModalOpen(true);
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
        )}
      </div>
    </TooltipProvider>
  );
};

// Component to redirect authenticated users to the app
function RedirectToApp() {
  const router = useRouter();
  
  useEffect(() => {
    router.push("/incidents");
  }, [router]);
  
  return null;
}

// Component to redirect unauthenticated users to sign-in
function RedirectToSignIn() {
  const router = useRouter();
  
  useEffect(() => {
    router.push("/sign-in");
  }, [router]);
  
  return null;
}

interface AppLayoutProps {
  children: React.ReactNode;
  isSettingsModalOpen: boolean;
  setIsSettingsModalOpen: (open: boolean) => void;
}

function AppLayout({
  children,
  isSettingsModalOpen,
  setIsSettingsModalOpen
}: AppLayoutProps) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, isLoaded } = useUser();
  const {
    isChatExpanded,
    setIsChatExpanded,
    isNavExpanded,
    setIsNavExpanded,
    isCodeSectionExpanded,
    setIsCodeSectionExpanded,
    onChatSessionSelect,
    onNewChat,
    currentChatSessionId,
    workspaceConfig,
    closeWorkspace,
    workspacePanelWidth,
    setWorkspacePanelWidth,
  } = useChatExpansion();

  const workspaceResizeState = useRef<{ startX: number; startWidth: number } | null>(null);
  const [isWorkspaceResizing, setIsWorkspaceResizing] = useState(false);

  // Handlers for sidebar strip buttons
  const handleSidebarToggle = () => {
    setIsNavExpanded(!isNavExpanded);
  };

  const handleNewChatClick = () => {
    if (pathname === "/chat") {
      // If already on chat page, force reload to trigger new chat
      window.location.href = "/chat?newChat=true";
    } else {
      // Otherwise, navigate to chat page for new chat
      router.push("/chat?newChat=true");
    }
  };

  const handleChatToggle = () => {
    setIsChatExpanded(!isChatExpanded);
  };

  const handleWorkspacePointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (!workspaceConfig) {
      return;
    }
    workspaceResizeState.current = {
      startX: event.clientX,
      startWidth: workspacePanelWidth,
    };
    setIsWorkspaceResizing(true);
  }, [workspaceConfig, workspacePanelWidth]);

  useEffect(() => {
    if (!isWorkspaceResizing) {
      return;
    }

    const handlePointerMove = (event: PointerEvent) => {
      if (!workspaceResizeState.current) return;
      const { startX, startWidth } = workspaceResizeState.current;
      const delta = startX - event.clientX;
      const viewportWidth = window.innerWidth || 1440;
      const minWidth = 400;
      const maxWidth = Math.max(minWidth, Math.min(1600, viewportWidth - 280));
      const nextWidth = Math.min(maxWidth, Math.max(minWidth, startWidth + delta));
      setWorkspacePanelWidth(nextWidth);
    };

    const handlePointerUp = () => {
      setIsWorkspaceResizing(false);
      workspaceResizeState.current = null;
    };

    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });

    return () => {
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [isWorkspaceResizing, setWorkspacePanelWidth]);

  // List of public routes that don't require authentication
  const publicRoutes = ["/", "/privacy", "/terms"];
  const isPublicRoute = publicRoutes.includes(pathname);

  // Wait for auth state to load before making routing decisions
  // This prevents redirects during the initial loading phase on page refresh
  if (!isLoaded) {
    return null;
  }

  const renderMainContent = (
    <>
      <div className={`flex-1 ${pathname === "/chat" ? "flex overflow-hidden" : "overflow-auto"}`}>
        {pathname === "/chat" ? (
          <>
            <div className="flex-1 overflow-auto">
              {children}
            </div>
            {workspaceConfig?.type === "iac" && (
              <>
                <div
                  className="hidden sm:flex w-3 cursor-col-resize flex-shrink-0 items-center justify-center group"
                  onPointerDown={handleWorkspacePointerDown}
                  title="Drag left/right to resize workspace️"
                >
                  <div className="flex flex-col gap-1">
                    <div className="w-1 h-1 rounded-full bg-muted-foreground/40 group-hover:bg-muted-foreground/70" />
                    <div className="w-1 h-1 rounded-full bg-muted-foreground/40 group-hover:bg-muted-foreground/70" />
                    <div className="w-1 h-1 rounded-full bg-muted-foreground/40 group-hover:bg-muted-foreground/70" />
                    <div className="w-1 h-1 rounded-full bg-muted-foreground/40 group-hover:bg-muted-foreground/70" />
                    <div className="w-1 h-1 rounded-full bg-muted-foreground/40 group-hover:bg-muted-foreground/70" />
                    <div className="w-1 h-1 rounded-full bg-muted-foreground/40 group-hover:bg-muted-foreground/70" />
                  </div>
                </div>
                <div
                  className="hidden sm:flex h-full flex-shrink-0 bg-background"
                  style={{ width: `${workspacePanelWidth}px` }}
                >
                  <IaCWorkspace
                    sessionId={workspaceConfig.sessionId}
                    onClose={closeWorkspace}
                    onSave={workspaceConfig.onSave}
                    onPlan={workspaceConfig.onPlan}
                  />
                </div>
              </>
            )}
          </>
        ) : (
          <div className="flex-1 overflow-auto">
            {children}
          </div>
        )}
      </div>
    </>
  )

  return (
    <>
      {user ? (
        // Authenticated users get full app layout
        isPublicRoute ? (
          <RedirectToApp />
        ) : (
          <div className="flex h-screen bg-background overflow-hidden">
            <Navigation 
              isChatExpanded={isChatExpanded}
              onChatExpandToggle={handleChatToggle}
              isExpanded={isNavExpanded}
              setIsExpanded={setIsNavExpanded}
              isCodeSectionExpanded={isCodeSectionExpanded}
              setIsCodeSectionExpanded={setIsCodeSectionExpanded}
              showCodeSection={true}
              onChatSessionSelect={onChatSessionSelect}
              onNewChat={onNewChat}
              currentChatSessionId={currentChatSessionId}
              onSettingsClick={() => setIsSettingsModalOpen(true)}
            />
            <main className={`flex-1 flex flex-col ${pathname === "/chat" ? "overflow-hidden" : "overflow-auto"}`} style={!isNavExpanded ? { marginLeft: '64px', width: 'calc(100% - 64px)' } : {}}>
              {renderMainContent}
            </main>
            
            <SidebarStrip 
              isNavExpanded={isNavExpanded}
              onSidebarToggle={handleSidebarToggle}
              onNewChatClick={handleNewChatClick}
              isSettingsModalOpen={isSettingsModalOpen}
              setIsSettingsModalOpen={setIsSettingsModalOpen}
            />
          </div>
        )
      ) : (
        // Unauthenticated users - show public pages or redirect to sign-in
        isPublicRoute ? children : <RedirectToSignIn />
      )}

      {/* Settings Modal */}
      <SettingsModal 
        isOpen={isSettingsModalOpen} 
        onClose={() => setIsSettingsModalOpen(false)} 
      />
    </>
  );
}

export default AppLayout;

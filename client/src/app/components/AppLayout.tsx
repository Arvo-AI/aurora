"use client";

import React, { useState, useRef, useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useUser } from "@/hooks/useAuthHooks";
import { signOut } from "next-auth/react";
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
                  <div className="h-10 w-10 rounded-full bg-muted flex items-center justify-center overflow-hidden">
                {user.imageUrl ? (
                  <img 
                    src={user.imageUrl} 
                    alt={user.fullName || "User"} 
                    className="w-full h-full object-cover"
                  />
                ) : (<span className="text-sm font-medium">
                  {(user.fullName || "U")[0].toUpperCase()}
                  </span>
                )}
                </div>
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
  } = useChatExpansion();

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
          <div className="flex-1 overflow-hidden">
            {children}
          </div>
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
            <main className={`flex-1 flex flex-col ${pathname === "/chat" ? "overflow-hidden" : "overflow-auto"}`} style={isNavExpanded ? { width: 'calc(100% - 224px)' } : { marginLeft: '64px', width: 'calc(100% - 64px)' }}>
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

"use client";

import React, { useState, useRef, useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useUser } from "@/hooks/useAuthHooks";
import { signOut } from "next-auth/react";
import dynamic from "next/dynamic";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Gauge, LogOut, Plug, Settings, SquarePen, Workflow, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
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

  const navItems = [
    { href: "/chat", label: "New Chat", icon: SquarePen, active: pathname?.startsWith("/chat") },
    { href: "/incidents", label: "Incidents", icon: Zap, active: pathname?.startsWith("/incidents"), showIndicator: true },
    { href: "/actions", label: "Actions", icon: Workflow, active: pathname?.startsWith("/actions") },
    { href: "/monitor", label: "Monitor", icon: Gauge, active: pathname?.startsWith("/monitor") },
    { href: "/connectors", label: "Connectors", icon: Plug, active: pathname === "/connectors" },
  ];

  return (
    <TooltipProvider>
      <div className="absolute inset-y-0 left-0 w-16 bg-card border-r border-border flex flex-col items-center pt-4 pb-6 z-50">
        <div className="flex flex-col items-center gap-6">
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

          <div className="flex flex-col items-center gap-1">
            {navItems.map(({ href, label, icon: Icon, active, showIndicator }) => (
              <Tooltip key={href}>
                <TooltipTrigger asChild>
                  <Link
                    href={href}
                    aria-label={label}
                    className={cn(
                      "relative h-8 w-8 rounded-md flex items-center justify-center transition-colors border border-transparent hover:bg-primary/10 hover:border-border/50",
                      active
                        ? "bg-card border-border shadow-sm text-foreground"
                        : "text-muted-foreground"
                    )}
                  >
                    <Icon size={16} className={showIndicator ? "text-foreground" : undefined} />
                    {showIndicator && (
                      <span className="absolute top-1.5 right-1.5 flex h-2 w-2">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-muted-foreground opacity-75" />
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-muted-foreground" />
                      </span>
                    )}
                  </Link>
                </TooltipTrigger>
                <TooltipContent side="right" sideOffset={5}>
                  <p>{label}</p>
                </TooltipContent>
              </Tooltip>
            ))}
          </div>
        </div>
        
        {user && (
          <div className="relative mt-auto" ref={userMenuRef}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  aria-label="User menu"
                  className="h-8 w-8 shrink-0 rounded-full p-0 shadow-md border border-border bg-card flex items-center justify-center overflow-hidden"
                  onClick={() => setIsUserMenuOpen(!isUserMenuOpen)}
                >
                  <Avatar className="h-8 w-8">
                    <AvatarImage src={user.imageUrl || undefined} alt="" />
                    <AvatarFallback className="bg-primary text-primary-foreground text-xs font-medium">
                      {(user.fullName || user.firstName || user.emailAddresses[0]?.emailAddress || "U")
                        .split(" ")
                        .map((name) => name[0])
                        .join("")
                        .toUpperCase()
                        .slice(0, 2)}
                    </AvatarFallback>
                  </Avatar>
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
          <div className="relative flex h-screen bg-background overflow-hidden">
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
            <main className={`flex-1 min-w-0 flex flex-col ${pathname === "/chat" ? "overflow-hidden" : "overflow-auto"}`}>
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

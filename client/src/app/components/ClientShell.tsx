"use client";

import React, { useState, useEffect, createContext, useContext, useCallback } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useUser } from "@/hooks/useAuthHooks";
import { signOut } from "next-auth/react";
import { ThemeProvider } from "next-themes";
import { ProviderPreferenceProvider } from "@/context/ProviderPreferenceContext";
import { Toaster } from "@/components/ui/toaster";
import { useToast } from "@/hooks/use-toast";
import AppLayout from "@/app/components/AppLayout";
import GlobalProjectSelectionMonitor from "@/components/cloud-provider/GlobalProjectSelectionMonitor";
import { WebViewWarning } from "@/components/WebViewWarning";

type WorkspaceConfig = {
  type: "iac"
  sessionId: string
  onSave?: (path: string, content: string) => Promise<boolean>
  onPlan?: () => Promise<boolean>
}

// Chat context definition (moved from layout.tsx)
export const ChatContext = createContext<{
  isChatExpanded: boolean;
  setIsChatExpanded: (value: boolean) => void;
  isNavExpanded: boolean;
  setIsNavExpanded: (value: boolean) => void;
  isCodeSectionExpanded: boolean;
  setIsCodeSectionExpanded: (value: boolean) => void;
  selectedProviders: string[];
  setSelectedProviders: (value: string[]) => void;
  onChatSessionSelect?: (sessionId: string) => void;
  onNewChat?: () => void;
  currentChatSessionId?: string | null;
  setOnChatSessionSelect: (handler: ((sessionId: string) => void) | undefined) => void;
  setOnNewChat: (handler: (() => void) | undefined) => void;
  setCurrentChatSessionId: (sessionId: string | null) => void;
  refreshChatHistory: () => void;
  setRefreshChatHistory: (refreshFn: () => void) => void;
  workspaceConfig: WorkspaceConfig | null;
  openWorkspace: (config: WorkspaceConfig) => void;
  closeWorkspace: () => void;
  workspacePanelWidth: number;
  setWorkspacePanelWidth: (width: number) => void;
}>({
  isChatExpanded: true,
  setIsChatExpanded: () => {},
  isNavExpanded: true,
  setIsNavExpanded: () => {},
  isCodeSectionExpanded: true,
  setIsCodeSectionExpanded: () => {},
  selectedProviders: ["gcp", "azure", "aws"],
  setSelectedProviders: () => {},
  onChatSessionSelect: undefined,
  onNewChat: undefined,
  currentChatSessionId: null,
  setOnChatSessionSelect: () => {},
  setOnNewChat: () => {},
  setCurrentChatSessionId: () => {},
  refreshChatHistory: () => {},
  setRefreshChatHistory: () => {},
  workspaceConfig: null,
  openWorkspace: () => {},
  closeWorkspace: () => {},
  workspacePanelWidth: 800, 
  setWorkspacePanelWidth: () => {},
});

// Custom hook to use chat context
export const useChatExpansion = () => useContext(ChatContext);

interface ClientShellProps {
  children: React.ReactNode;
}

export default function ClientShell({ children }: ClientShellProps) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, isLoaded } = useUser();

  // All the state that was in layout.tsx
  const [isChatExpanded, setIsChatExpanded] = useState(true);
  const [isNavExpanded, setIsNavExpanded] = useState(() => {
    if (typeof window === "undefined") return true;
    const stored = localStorage.getItem("aurora_nav_expanded");
    return stored ? JSON.parse(stored) : true;
  });
  const [isCodeSectionExpanded, setIsCodeSectionExpanded] = useState(true);
  const [selectedProviders, setSelectedProviders] = useState<string[]>(["gcp", "azure", "aws"]);
  const [onChatSessionSelect, setOnChatSessionSelect] = useState<((sessionId: string) => void) | undefined>(undefined);
  const [onNewChat, setOnNewChat] = useState<(() => void) | undefined>(undefined);
  const [currentChatSessionId, setCurrentChatSessionId] = useState<string | null>(null);
  const [refreshChatHistory, setRefreshChatHistory] = useState<(() => void)>(() => () => {});
  const [isSettingsModalOpen, setIsSettingsModalOpen] = useState(false);
  const [workspaceConfig, setWorkspaceConfig] = useState<WorkspaceConfig | null>(null);
  const [workspacePanelWidth, setWorkspacePanelWidth] = useState(800); 

  const { toast } = useToast();

  // Clear localStorage if version has changed (moved from layout.tsx)
  useEffect(() => {
    if (typeof window === "undefined") return;
    
    const APP_VERSION = "1.2.1"; 
    const STORAGE_VERSION_KEY = "aurora_app_version";
    
    const storedVersion = localStorage.getItem(STORAGE_VERSION_KEY);
    
    if (storedVersion !== APP_VERSION) {
      console.log("App version changed, clearing localStorage to fix potential bugs");
      localStorage.clear();
      localStorage.setItem(STORAGE_VERSION_KEY, APP_VERSION);
    }
  }, []);

  // Force GCP reauthentication when service account naming changes
  useEffect(() => {
    if (typeof window === "undefined") return;
    
    const GCP_AUTH_VERSION = "1.0.0"; // Bump this to force GCP reauth
    const GCP_AUTH_VERSION_KEY = "aurora_gcp_auth_version";
    
    const storedGcpAuthVersion = localStorage.getItem(GCP_AUTH_VERSION_KEY);
    
    // Force reauth if version key doesn't exist (first deployment) or version changed
    if (!storedGcpAuthVersion || storedGcpAuthVersion !== GCP_AUTH_VERSION) {
      console.log(`GCP authentication version ${!storedGcpAuthVersion ? 'not set' : 'changed'}, forcing reauth`);
      
      // Clear GCP-specific localStorage keys
      const gcpKeys = [
        'isGCPConnected',
        'isGCPFetched',
        'isGCPFetching',
        'cloudProvider',
        'gcpSetupInProgress',
        'gcpSetupTaskId',
        'gcpPollingActive',
        'gcpSetupInProgress_timestamp',
        'gcpPollingIntervalId'
      ];
      
      gcpKeys.forEach(key => localStorage.removeItem(key));
      
      // Clear provider preferences cache if it includes GCP
      try {
        const cachedPrefs = localStorage.getItem('provider_preferences_cache');
        if (cachedPrefs) {
          const prefs = JSON.parse(cachedPrefs);
          if (Array.isArray(prefs) && prefs.includes('gcp')) {
            const filtered = prefs.filter((p: string) => p !== 'gcp');
            localStorage.setItem('provider_preferences_cache', JSON.stringify(filtered));
          }
        }
      } catch (e) {
        console.warn('Failed to update provider preferences cache:', e);
      }
      
      // Delete GCP tokens from backend
      fetch('/api/gcp/force-disconnect', { method: 'POST' })
        .then(res => {
          if (res.ok) {
            console.log('GCP tokens deleted from backend');
            
            // Show notification to user
            toast({
              title: "GCP Reconnection Required",
              description: "We've updated our GCP integration. Please reconnect your GCP account.",
              duration: 8000,
            });
            
            // Dispatch event to update provider UI
            window.dispatchEvent(new CustomEvent('providerStateChanged'));
          }
        })
        .catch(err => console.error('Failed to delete GCP tokens:', err));
      
      localStorage.setItem(GCP_AUTH_VERSION_KEY, GCP_AUTH_VERSION);
    }
  }, [toast]);

  // Save sidebar state to localStorage (moved from layout.tsx)
  useEffect(() => {
    if (typeof window === "undefined") return;
    localStorage.setItem("aurora_nav_expanded", JSON.stringify(isNavExpanded));
  }, [isNavExpanded]);


  // Chat context value
  const chatContextValue = {
    isChatExpanded,
    setIsChatExpanded,
    isNavExpanded,
    setIsNavExpanded,
    isCodeSectionExpanded,
    setIsCodeSectionExpanded,
    selectedProviders,
    setSelectedProviders,
    onChatSessionSelect,
    onNewChat,
    currentChatSessionId,
    setOnChatSessionSelect,
    setOnNewChat,
    setCurrentChatSessionId,
    refreshChatHistory,
    setRefreshChatHistory,
    workspaceConfig,
    openWorkspace: useCallback((config: WorkspaceConfig) => {
      setWorkspaceConfig(config);
    }, []),
    closeWorkspace: useCallback(() => {
      setWorkspaceConfig(null);
    }, []),
    workspacePanelWidth,
    setWorkspacePanelWidth,
  };

  return (
    <ThemeProvider attribute="class" defaultTheme="dark">
      <ProviderPreferenceProvider>
        <ChatContext.Provider value={chatContextValue}>
          <AppLayout
            isSettingsModalOpen={isSettingsModalOpen}
            setIsSettingsModalOpen={setIsSettingsModalOpen}
          >
            {children}
          </AppLayout>
          <Toaster />
          <WebViewWarning />
          <GlobalProjectSelectionMonitor />
        </ChatContext.Provider>
      </ProviderPreferenceProvider>
    </ThemeProvider>
  );
}

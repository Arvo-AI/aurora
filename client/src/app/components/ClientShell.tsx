"use client";

import React, { useState, useEffect, createContext, useContext } from "react";
import { ThemeProvider } from "next-themes";
import { ProviderPreferenceProvider } from "@/context/ProviderPreferenceContext";
import { Toaster } from "@/components/ui/toaster";
import AppLayout from "@/app/components/AppLayout";
import GlobalProjectSelectionMonitor from "@/components/cloud-provider/GlobalProjectSelectionMonitor";
import { WebViewWarning } from "@/components/WebViewWarning";
import OnboardingConnectorBar from "@/components/OnboardingConnectorBar";
import { useConnectionHealth } from "@/hooks/useConnectionHealth";

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
});

// Custom hook to use chat context
export const useChatExpansion = () => useContext(ChatContext);

interface ClientShellProps {
  children: React.ReactNode;
}

export default function ClientShell({ children }: ClientShellProps) {
  useConnectionHealth();

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
          <OnboardingConnectorBar />
          <GlobalProjectSelectionMonitor />
        </ChatContext.Provider>
      </ProviderPreferenceProvider>
    </ThemeProvider>
  );
}

"use client";

import { MutableRefObject, useCallback, useEffect, useRef, useState, startTransition } from "react";
import { useToast } from "@/hooks/use-toast";
import { providerPreferencesService } from "@/lib/services/providerPreferences";
import { useCloudProviderStatus } from "@/hooks/use-cloud-provider-status";
import { Message } from "../types";
import { AppRouterInstance } from "next/dist/shared/lib/app-router-context.shared-runtime";

export type ChatWebSocket = {
  send: (payload: any) => boolean;
  isReady: boolean;
};

interface ChatSendHandlerParams {
  userId: string | null;
  currentSessionId: string | null;
  setCurrentSessionId: (sessionId: string | null) => void;
  hasCreatedSession: boolean;
  setHasCreatedSession: (value: boolean) => void;
  createSession: (title: string) => Promise<string | null>;
  router: AppRouterInstance;
  onNewMessage: (message: Message) => void;
  justCreatedSessionRef: MutableRefObject<string | null>;
  onSessionCreated?: (sessionId: string) => void;
  images?: Array<{file: File, preview: string}>;
}

interface ChatSendHandlerResult {
  selectedModel: string;
  setSelectedModel: (model: string) => void;
  selectedMode: string;
  setSelectedMode: (mode: string) => void;
  selectedProviders: string[];
  setSelectedProviders: (providers: string[]) => void;
  isSending: boolean;
  setIsSending: (value: boolean) => void;
  handleSend: (messageText: string, socket: ChatWebSocket, overrideMode?: string) => Promise<boolean>;
  handlePromptClick: (prompt: string, socket: ChatWebSocket) => void;
}

const normalizeMode = (mode?: string | null) => (mode || "agent").toLowerCase();

const arraysEqual = (a: string[], b: string[]) => {
  if (a.length !== b.length) return false;
  // Order-independent comparison: sort both arrays before comparing
  const sortedA = [...a].sort();
  const sortedB = [...b].sort();
  return sortedA.every((value, index) => value === sortedB[index]);
};

export function useChatSendHandlers({
  userId,
  currentSessionId,
  setCurrentSessionId,
  hasCreatedSession,
  setHasCreatedSession,
  createSession,
  router,
  onNewMessage,
  justCreatedSessionRef,
  onSessionCreated,
  images = [],
}: ChatSendHandlerParams): ChatSendHandlerResult {
  const { toast } = useToast();
  const { 
    anyProviderConnected,
    isGCPConnected,
    isAWSConnected,
    isAzureConnected,
    isOVHConnected,
    isScalewayConnected
  } = useCloudProviderStatus();

  const [selectedModel, setSelectedModel] = useState("openai/gpt-5.2");
  const [selectedMode, setSelectedMode] = useState("agent");
  const [selectedProviders, setSelectedProviders] = useState<string[]>([]);
  const [isSending, setIsSending] = useState(false);
  const isSyncingRef = useRef(false);

  // Get list of connected providers
  const getConnectedProviders = useCallback(() => {
    const connected: string[] = [];
    if (isGCPConnected) connected.push('gcp');
    if (isAWSConnected) connected.push('aws');
    if (isAzureConnected) connected.push('azure');
    if (isOVHConnected) connected.push('ovh');
    if (isScalewayConnected) connected.push('scaleway');
    return connected;
  }, [isGCPConnected, isAWSConnected, isAzureConnected, isOVHConnected, isScalewayConnected]);

  const syncProvidersWithMode = useCallback((modeValue: string, providers: string[]) => {
    // Prevent infinite loop - if we're already syncing, skip
    if (isSyncingRef.current) {
      return providers;
    }

    isSyncingRef.current = true;
    
    // Providers are already validated by the API, so we can use them directly
    setSelectedProviders(currentProviders => {
      if (!arraysEqual(providers, currentProviders)) {
        // Only persist if actually changed - use setTimeout to avoid blocking
        setTimeout(() => {
          providerPreferencesService.setProviderPreferences(providers).catch((error) => {
            console.error('Failed to persist provider preferences:', error);
          });
          // Reset sync flag after async operation
          isSyncingRef.current = false;
        }, 0);
        // Don't dispatch event here - it causes infinite loop
        // The event should only be dispatched from external sources, not from internal state updates
        return providers;
      }
      // Reset sync flag if no change
      isSyncingRef.current = false;
      return currentProviders;
    });

    return providers;
  }, []); // No dependencies - uses functional setState

  useEffect(() => {
    const loadProviderPreferences = async () => {
      try {
        const providers = await providerPreferencesService.getProviderPreferences();
        syncProvidersWithMode(selectedMode, providers);
      } catch (error) {
        console.error('Failed to load provider preferences:', error);
        setSelectedProviders([]);
      }
    };

    loadProviderPreferences();

    const handleProviderChange = (event: CustomEvent) => {
      if (event.detail?.providers) {
        const newProviders = event.detail.providers;
        // Only sync if providers actually changed to prevent loop
        setSelectedProviders(currentProviders => {
          if (!arraysEqual(newProviders, currentProviders)) {
            isSyncingRef.current = true;
            // Persist the new preferences
            setTimeout(() => {
              providerPreferencesService.setProviderPreferences(newProviders).catch((error) => {
                console.error('Failed to persist provider preferences:', error);
              });
              isSyncingRef.current = false;
            }, 0);
            return newProviders;
          }
          return currentProviders;
        });
      }
    };

    window.addEventListener('providerPreferenceChanged', handleProviderChange as EventListener);
    return () => {
      window.removeEventListener('providerPreferenceChanged', handleProviderChange as EventListener);
    };
  }, [selectedMode, syncProvidersWithMode]);

  const sendMessage = useCallback(async (
    messageText: string,
    socket: ChatWebSocket,
    modeOverride?: string,
    providersOverride?: string[],
  ) => {
    const trimmed = messageText.trim();
    if (!trimmed || isSending || !userId) return false;

    const effectiveMode = normalizeMode(modeOverride || selectedMode);
    const connectedProviders = getConnectedProviders();
    const providersForMode = providersOverride ?? selectedProviders;
    const finalProviders = syncProvidersWithMode(effectiveMode, providersForMode);

    if (!socket.isReady) {
      toast({
        description: "Connection not ready. Please wait a moment and try again.",
        variant: "destructive"
      });
      return false;
    }

    setIsSending(true);

    const userMessage: Message = {
      id: Date.now(),
      sender: "user",
      text: trimmed,
      images: images.length > 0 ? images.map(img => ({
        data: img.preview.split(',')[1],
        displayData: img.preview,
        name: img.file.name,
        type: img.file.type
      })) : undefined
    };

    let actualSessionId = currentSessionId;
    if (!hasCreatedSession) {
      try {
        const title = trimmed.length > 50 ? trimmed.substring(0, 50).trimEnd() + '...' : trimmed;
        const newSessionId = await createSession(title);
        if (newSessionId) {
          actualSessionId = newSessionId;
          onSessionCreated?.(newSessionId);

          setCurrentSessionId(newSessionId);
          setHasCreatedSession(true);
          justCreatedSessionRef.current = newSessionId;

          startTransition(() => {
            router.replace(`/chat?sessionId=${newSessionId}`);
          });
        }
      } catch (error) {
        console.error('Error creating session:', error);
      }
    }

    onNewMessage(userMessage);

    // Convert images to attachments
    const attachments = await Promise.all(
      images.map(async (img) => {
        const base64 = img.preview.split(',')[1]; // Remove data URL prefix
        return {
          file_type: img.file.type,
          file_data: base64,
          filename: img.file.name
        };
      })
    );

    socket.send({
      type: 'message',
      query: trimmed,
      user_id: userId,
      session_id: actualSessionId || undefined,
      model: selectedModel,
      mode: effectiveMode,
      provider_preference: finalProviders,
      attachments: attachments.length > 0 ? attachments : undefined,
      ui_state: {
        selectedModel,
        selectedMode: effectiveMode,
        selectedProviders: finalProviders
      }
    });

    return true;
  }, [
    createSession, currentSessionId, hasCreatedSession, justCreatedSessionRef,
    onNewMessage, router, selectedMode, selectedModel, selectedProviders,
    syncProvidersWithMode, toast, userId, setCurrentSessionId, 
    setHasCreatedSession, isSending, getConnectedProviders, images
  ]);

  const initiateSend = useCallback(async (messageText: string, socket: ChatWebSocket, modeOverride?: string) => {
    const trimmed = messageText.trim();
    if (!trimmed) return false;

    const targetMode = modeOverride || selectedMode;
    // Show warning if no providers connected
    if (!anyProviderConnected) {
      toast({
        description: "No cloud provider selected. Connect a provider for reliable execution.",
        variant: "default",
      });
    }

    // Let sendMessage handle provider enforcement and normalization
    return await sendMessage(messageText, socket, targetMode);
  }, [anyProviderConnected, selectedMode, sendMessage, toast]);

  const handleSend = useCallback(async (messageText: string, socket: ChatWebSocket, overrideMode?: string) => {
    return await initiateSend(messageText, socket, overrideMode);
  }, [initiateSend]);

  const handlePromptClick = useCallback((prompt: string, socket: ChatWebSocket) => {
    setTimeout(() => {
      initiateSend(prompt, socket);
    }, 100);
  }, [initiateSend]);

  return {
    selectedModel,
    setSelectedModel,
    selectedMode,
    setSelectedMode,
    selectedProviders,
    setSelectedProviders,
    isSending,
    setIsSending,
    handleSend,
    handlePromptClick,
  };
}

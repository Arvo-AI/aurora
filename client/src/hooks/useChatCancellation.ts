"use client";

import { useCallback, useRef } from "react";
import { toast } from "@/hooks/use-toast";

export interface ChatCancellationConfig {
  userId?: string | null;
  sessionId?: string | null;
  webSocket: {
    isConnected: boolean;
    send: (message: any) => boolean;
  };
  // Add access to actual WebSocket for better state checking
  wsRef?: React.MutableRefObject<WebSocket | null>;
}

export interface ChatCancellation {
  cancelCurrentMessage: () => Promise<void>;
  canCancel: boolean;
}

export const useChatCancellation = ({
  userId,
  sessionId,
  webSocket,
  wsRef
}: ChatCancellationConfig): ChatCancellation => {
  const cancelRequestRef = useRef<boolean>(false);

  const cancelCurrentMessage = useCallback(async () => {
    if (!sessionId) {
      console.warn('Cannot cancel: no session');
      return;
    }

    // Set cancel request flag to prevent duplicate cancel requests
    if (cancelRequestRef.current) {
      console.log('Cancel request already in progress');
      return;
    }

    cancelRequestRef.current = true;

    try {
      // Check actual WebSocket readyState instead of React state
      const actualReadyState = wsRef?.current?.readyState;
      const isActuallyOpen = actualReadyState === WebSocket.OPEN;
      
      if (!isActuallyOpen) {
        // WebSocket is not actually open - show user-friendly message
        console.warn('WebSocket not available for cancellation:', {
          readyState: actualReadyState,
          stateNames: {
            0: 'CONNECTING',
            1: 'OPEN', 
            2: 'CLOSING',
            3: 'CLOSED'
          }[actualReadyState || 3]
        });
        
        toast({
          title: "Connection unavailable",
          description: "Please try again in a moment when the connection is restored.",
          variant: "default",
        });
        
        cancelRequestRef.current = false;
        return;
      }

      // Send cancel message to backend via WebSocket
      const cancelMessage = {
        type: 'control',
        action: 'cancel',
        session_id: sessionId,
        user_id: userId
      };

      console.log('Sending cancel message:', cancelMessage);
      
      const sent = webSocket.send(cancelMessage);
      if (!sent) {
        // This shouldn't happen since we checked readyState, but handle it anyway
        console.warn('WebSocket send returned false despite OPEN state');
        
        toast({
          title: "Cancellation failed",
          description: "Please try again in a moment.",
          variant: "default",
        });
        
        cancelRequestRef.current = false;
        return;
      }
      
      console.log('Cancel message sent successfully');
      
      // Reset the flag after a brief delay to allow new cancel requests
      setTimeout(() => {
        cancelRequestRef.current = false;
      }, 1000);

    } catch (error) {
      console.error('Failed to send cancel message:', error);
      
      toast({
        title: "Cancellation error",
        description: "An unexpected error occurred. Please try again.",
        variant: "destructive",
      });
      
      cancelRequestRef.current = false;
    }
  }, [webSocket, sessionId, userId, wsRef]);

  // More accurate canCancel check using actual WebSocket state
  const canCancel = sessionId && 
                    (wsRef?.current?.readyState === WebSocket.OPEN || webSocket.isConnected);

  return {
    cancelCurrentMessage,
    canCancel: !!canCancel
  };
};

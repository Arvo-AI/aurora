import React, { Suspense } from "react";
import ChatClient from "./components/ChatClient";

// Server component for chat page
export default async function ChatPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  // Await searchParams as required in Next.js 15+
  const params = await searchParams;
  
  // Extract server-side searchParams
  const sessionId = typeof params.sessionId === 'string' ? params.sessionId : undefined;
  const newChat = typeof params.newChat === 'string' ? params.newChat === 'true' : false;
  const initialMessage = typeof params.message === 'string' ? decodeURIComponent(params.message) : undefined;
  const initialMode = typeof params.mode === 'string' ? params.mode : undefined;
  
  let incidentContext: string | undefined;
  try {
    if (typeof params.incident === 'string') {
      // Decode from base64
      incidentContext = decodeURIComponent(Buffer.from(params.incident, 'base64').toString());
    }
  } catch (e) {
    console.error('Failed to decode incident context:', e);
    incidentContext = undefined;
  }

  return (
    <div className="flex flex-col h-full bg-background overflow-hidden">
      <Suspense 
        fallback={
          <div className="flex h-screen items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-muted-foreground" />
            <span className="ml-2 text-muted-foreground">Loading chat...</span>
          </div>
        }
      >
        <ChatClient
          initialSessionId={sessionId}
          shouldStartNewChat={newChat}
          initialMessage={initialMessage}
          incidentContext={incidentContext}
          initialMode={initialMode}
        />
      </Suspense>
    </div>
  );
}
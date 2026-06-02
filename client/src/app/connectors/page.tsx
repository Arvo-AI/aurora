import { Suspense } from "react";
import ConnectorsClient from "./components/ConnectorsClient";

export default async function ConnectorsPage() {
  return (
    <div className="flex flex-col h-screen bg-background">
      <Suspense 
        fallback={
          <div className="flex h-screen items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-muted-foreground" />
            <span className="ml-2 text-muted-foreground">Loading connectors...</span>
          </div>
        }
      >
        <ConnectorsClient />
      </Suspense>
    </div>
  );
}

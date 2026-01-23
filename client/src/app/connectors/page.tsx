import { Suspense } from "react";
import { Loader2 } from "lucide-react";
import ConnectorsClient from "./components/ConnectorsClient";

export default async function ConnectorsPage() {
  return (
    <div className="flex flex-col h-screen bg-background">
      <Suspense 
        fallback={
          <div className="flex h-screen items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin" />
            <span className="ml-2 text-muted-foreground">Loading connectors...</span>
          </div>
        }
      >
        <ConnectorsClient />
      </Suspense>
    </div>
  );
}

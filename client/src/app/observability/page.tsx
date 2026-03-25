import { Suspense } from "react";
import { Loader2 } from "lucide-react";
import ObservabilityClient from "./components/ObservabilityClient";

export default function ObservabilityPage() {
  return (
    <div className="flex flex-col h-screen bg-background">
      <Suspense
        fallback={
          <div className="flex h-screen items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin" />
            <span className="ml-2 text-muted-foreground">Loading observability...</span>
          </div>
        }
      >
        <ObservabilityClient />
      </Suspense>
    </div>
  );
}

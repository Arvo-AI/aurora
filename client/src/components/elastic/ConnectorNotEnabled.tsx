"use client";

import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Power } from "lucide-react";

export function ConnectorNotEnabled() {
  const router = useRouter();
  return (
    <div className="flex items-center justify-center min-h-[60vh]">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <Power className="h-6 w-6 text-muted-foreground" />
          </div>
          <CardTitle className="text-lg">This connector is not enabled</CardTitle>
        </CardHeader>
        <CardContent className="text-center space-y-4">
          <p className="text-sm text-muted-foreground">
            The Elastic Cloud connector is behind a feature flag. Set <code>NEXT_PUBLIC_ENABLE_ELASTIC=true</code> in your Aurora environment and restart the stack to enable it.
          </p>
          <Button variant="outline" onClick={() => router.push("/connectors")}>Back to connectors</Button>
        </CardContent>
      </Card>
    </div>
  );
}

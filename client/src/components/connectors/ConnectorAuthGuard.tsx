"use client";

import { useUser } from "@/hooks/useAuthHooks";
import { canWrite } from "@/lib/roles";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Lock } from "lucide-react";
import { useRouter } from "next/navigation";

interface ConnectorAuthGuardProps {
  children: React.ReactNode;
  connectorName?: string;
}

export function useCanWriteConnectors() {
  const { user, isLoaded } = useUser();
  return { canWrite: canWrite(user?.role), isLoaded, role: user?.role };
}

export default function ConnectorAuthGuard({
  children,
  connectorName,
}: ConnectorAuthGuardProps) {
  const { canWrite, isLoaded } = useCanWriteConnectors();
  const router = useRouter();

  if (!isLoaded) return null;

  if (!canWrite) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
              <Lock className="h-6 w-6 text-muted-foreground" />
            </div>
            <CardTitle className="text-lg">Permission Required</CardTitle>
          </CardHeader>
          <CardContent className="text-center space-y-4">
            <p className="text-sm text-muted-foreground">
              You need <strong>Editor</strong> or <strong>Admin</strong> role to
              manage{connectorName ? ` ${connectorName}` : ""} connections.
              Contact your organization admin to request access.
            </p>
            <Button variant="outline" onClick={() => router.push("/connectors")}>
              Back to Connectors
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return <>{children}</>;
}

"use client";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CheckCircle2 } from "lucide-react";
import { VictorOpsStatus } from "@/lib/services/victorops";

interface VictorOpsConnectedViewProps {
  readonly status: VictorOpsStatus;
  readonly onDisconnect: () => void;
  readonly disconnecting: boolean;
}

export function VictorOpsConnectedView({
  status,
  onDisconnect,
  disconnecting,
}: VictorOpsConnectedViewProps) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-4">
        <div className="flex items-center gap-3">
          <CheckCircle2 className="h-5 w-5 text-green-500" />
          <CardTitle className="text-base">
            {status.displayName ?? "Splunk On-Call"}
          </CardTitle>
          <Badge variant="secondary" className="text-green-700 bg-green-100 dark:bg-green-900/30 dark:text-green-400">
            Connected
          </Badge>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={onDisconnect}
          disabled={disconnecting}
          className="text-destructive hover:text-destructive"
        >
          {disconnecting ? "Disconnecting…" : "Disconnect"}
        </Button>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {status.externalUserName && (
          <div className="flex justify-between">
            <span className="text-muted-foreground">Account</span>
            <span className="font-medium">{status.externalUserName}</span>
          </div>
        )}
        {status.validatedAt && (
          <div className="flex justify-between">
            <span className="text-muted-foreground">Last validated</span>
            <span className="font-medium">
              {new Date(status.validatedAt).toLocaleString()}
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

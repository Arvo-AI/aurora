"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Check, X } from "lucide-react";
import type { PagerDutyStatus } from "@/lib/services/pagerduty";

interface PagerDutyConnectedViewProps {
  status: PagerDutyStatus;
  onChangeToken: () => void;
  onDisconnect: () => void;
  loading: boolean;
}

export function PagerDutyConnectedView({
  status,
  onChangeToken,
  onDisconnect,
  loading,
}: PagerDutyConnectedViewProps) {

  const formatDate = (dateString?: string) => {
    if (!dateString) return "just now";
    try {
      return new Date(dateString).toLocaleString();
    } catch {
      return "recently";
    }
  };

  const capabilities = status.capabilities || {
    can_read_incidents: false,
    can_write_incidents: false,
  };

  const authTypeLabel = status.authType === 'oauth' ? 'OAuth 2.0' : 'API Token';
  const authTypeColor = status.authType === 'oauth' 
    ? 'bg-blue-50 dark:bg-blue-950/20 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-800'
    : 'bg-purple-50 dark:bg-purple-950/20 text-purple-700 dark:text-purple-400 border-purple-200 dark:border-purple-800';

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>PagerDuty</CardTitle>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className={authTypeColor}>
              {authTypeLabel}
            </Badge>
            <Badge variant="outline" className="bg-green-50 dark:bg-green-950/20 text-green-700 dark:text-green-400 border-green-200 dark:border-green-800">
              <Check className="h-3 w-3 mr-1" />
              Connected
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Connection Info */}
        <div className="grid md:grid-cols-2 gap-4">
          {(status.externalUserEmail || status.externalUserName) && (
            <div className="p-4 border rounded-lg">
              <p className="text-xs text-muted-foreground uppercase tracking-wide">User</p>
              <p className="text-base font-semibold break-all">
                {status.externalUserEmail || status.externalUserName}
              </p>
            </div>
          )}
          {status.accountSubdomain && (
            <div className="p-4 border rounded-lg">
              <p className="text-xs text-muted-foreground uppercase tracking-wide">Account</p>
              <p className="text-base font-semibold">
                {status.accountSubdomain}.pagerduty.com
              </p>
            </div>
          )}
        </div>

        {/* Last Validated */}
        <div className="p-4 border rounded-lg bg-muted/40">
          <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">Last Validated</p>
          <p className="text-sm font-medium">{formatDate(status.validatedAt)}</p>
        </div>

        {/* Capabilities */}
        <div className="space-y-3">
          <p className="text-sm font-medium">Capabilities</p>
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm">
              {capabilities.can_read_incidents ? (
                <Check className="h-4 w-4 text-green-600" />
              ) : (
                <X className="h-4 w-4 text-muted-foreground" />
              )}
              <span className={capabilities.can_read_incidents ? "text-foreground" : "text-muted-foreground"}>
                Read incidents
              </span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              {capabilities.can_write_incidents ? (
                <Check className="h-4 w-4 text-green-600" />
              ) : (
                <X className="h-4 w-4 text-red-600" />
              )}
              <span className={capabilities.can_write_incidents ? "text-foreground" : "text-red-600 font-medium"}>
                Write incidents {!capabilities.can_write_incidents && "- not allowed with this token"}
              </span>
            </div>
          </div>
        </div>

        {/* Actions */}
        <div className="flex flex-col md:flex-row gap-3 pt-4 border-t">
          {status.authType === 'api_token' && (
            <Button
              variant="outline"
              onClick={onChangeToken}
              disabled={loading}
            >
              Rotate Token
            </Button>
          )}
          {status.authType === 'oauth' && (
            <p className="text-xs text-muted-foreground py-2">
              OAuth tokens refresh automatically. To update permissions, disconnect and reconnect.
            </p>
          )}
          <Button
            variant="outline"
            onClick={onDisconnect}
            disabled={loading}
            className="text-destructive hover:text-destructive"
          >
            {loading ? "Disconnecting…" : "Disconnect"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}


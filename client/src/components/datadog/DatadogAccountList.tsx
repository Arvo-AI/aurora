"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { DatadogAccount } from "@/lib/services/datadog";

interface DatadogAccountListProps {
  accounts: DatadogAccount[];
  onRemove: (label: string) => void;
  removingLabel: string | null;
  disabled?: boolean;
}

/**
 * Connected Datadog organizations. Each row is one org with its own key pair, so
 * validity is reported per row: a revoked key in one org must be visible rather
 * than hidden behind the others.
 */
export function DatadogAccountList({ accounts, onRemove, removingLabel, disabled }: DatadogAccountListProps) {
  if (accounts.length === 0) return null;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium">
          Connected {accounts.length === 1 ? 'Organization' : 'Organizations'}
        </p>
        {accounts.length > 1 && (
          <p className="text-xs text-muted-foreground">
            <strong>{accounts[0].label}</strong> is queried when no organization is specified
          </p>
        )}
      </div>

      <div className="border rounded-lg divide-y">
        {accounts.map((account) => (
          <div key={account.label} className="p-4 flex flex-wrap items-center gap-3">
            <div className="flex-1 min-w-[12rem] space-y-1">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{account.label}</span>
                {account.valid === false ? (
                  <Badge variant="destructive">Keys invalid</Badge>
                ) : (
                  <Badge variant="secondary">Connected</Badge>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                {account.site || 'datadoghq.com'}
                {account.orgName ? ` · ${account.orgName}` : ''}
                {account.serviceAccountName ? ` · ${account.serviceAccountName}` : ''}
              </p>
              {account.error && (
                <p className="text-xs text-destructive">{account.error}</p>
              )}
            </div>
            <Button
              variant="outline"
              size="sm"
              // Any removal in flight disables every button: the handler keys on a
              // single removingLabel, so a second concurrent remove would overwrite it.
              disabled={disabled || removingLabel !== null}
              onClick={() => onRemove(account.label)}
            >
              {removingLabel === account.label ? 'Removing…' : 'Remove'}
            </Button>
          </div>
        ))}
      </div>
    </div>
  );
}

"use client";

import { useState, useEffect, useCallback } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { useUser } from "@/hooks/useAuthHooks";
import { isAdmin } from "@/lib/roles";
import { Loader2, RotateCcw } from "lucide-react";

const ROLES = ["orchestrator", "subagent", "triage", "judge"] as const;
type ModelRole = (typeof ROLES)[number];

const PROVIDERS = [
  "anthropic",
  "openai",
  "google",
  "vertex",
  "ollama",
  "openrouter",
] as const;

const ROLE_LABEL: Record<ModelRole, string> = {
  orchestrator: "Orchestrator",
  subagent: "Subagent",
  triage: "Triage",
  judge: "Judge",
};

const ROLE_DESCRIPTION: Record<ModelRole, string> = {
  orchestrator: "Plans, ranks, synthesizes — heavier model.",
  subagent: "Executes individual investigation steps and tool calls.",
  triage: "Fast classification of incoming alerts and signals.",
  judge: "Scores and validates outputs from other agents.",
};

const MODEL_ID_MAX = 255;

interface Binding {
  role: ModelRole;
  provider: string;
  model_id: string;
  updated_at: string | null;
  is_default?: boolean;
}

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 60) return `updated ${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `updated ${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `updated ${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `updated ${diffDay}d ago`;
  const diffMo = Math.floor(diffDay / 30);
  if (diffMo < 12) return `updated ${diffMo}mo ago`;
  return `updated ${Math.floor(diffMo / 12)}y ago`;
}

function emptyBindings(): Binding[] {
  return ROLES.map((role) => ({
    role,
    provider: "anthropic",
    model_id: "",
    updated_at: null,
    is_default: true,
  }));
}

export function ModelRoleSettings() {
  const { user, isLoaded } = useUser();
  const admin = isAdmin(user?.role);
  const userLoading = !isLoaded;
  const { toast } = useToast();

  const [bindings, setBindings] = useState<Binding[]>(emptyBindings());
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [resettingRole, setResettingRole] = useState<ModelRole | null>(null);

  const fetchBindings = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await fetch("/api/settings/model-roles");
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "Failed to load model role bindings");
      }
      const data = await res.json();
      const incoming: Binding[] = Array.isArray(data.bindings) ? data.bindings : [];
      // Ensure all 4 roles present in canonical order.
      const byRole = new Map<ModelRole, Binding>(
        incoming.map((b) => [b.role, b] as const)
      );
      const merged: Binding[] = ROLES.map((role) => {
        const b = byRole.get(role);
        if (b) {
          return {
            role,
            provider: b.provider || "anthropic",
            model_id: b.model_id || "",
            updated_at: b.updated_at ?? null,
            is_default: b.is_default ?? false,
          };
        }
        return {
          role,
          provider: "anthropic",
          model_id: "",
          updated_at: null,
          is_default: true,
        };
      });
      setBindings(merged);
    } catch (error) {
      toast({
        title: "Failed to load",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    fetchBindings();
  }, [fetchBindings]);

  const updateRow = (role: ModelRole, patch: Partial<Binding>) => {
    setBindings((prev) =>
      prev.map((b) => (b.role === role ? { ...b, ...patch } : b))
    );
  };

  const handleSaveAll = async () => {
    // Basic non-empty validation.
    for (const b of bindings) {
      if (!b.provider.trim() || !b.model_id.trim()) {
        toast({
          title: "Missing values",
          description: `${ROLE_LABEL[b.role]} needs a provider and model ID.`,
          variant: "destructive",
        });
        return;
      }
    }
    setIsSaving(true);
    try {
      const res = await fetch("/api/settings/model-roles", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bindings: bindings.map((b) => ({
            role: b.role,
            provider: b.provider.trim(),
            model_id: b.model_id.trim(),
          })),
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "Failed to save model role bindings");
      }
      toast({
        title: "Saved",
        description: "Model role bindings updated.",
      });
      await fetchBindings();
    } catch (error) {
      toast({
        title: "Save failed",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  const handleReset = async (role: ModelRole) => {
    setResettingRole(role);
    try {
      const res = await fetch(
        `/api/settings/model-roles/${encodeURIComponent(role)}`,
        { method: "DELETE" }
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "Failed to reset binding");
      }
      toast({
        title: "Reset to default",
        description: `${ROLE_LABEL[role]} reverted to the system default.`,
      });
      await fetchBindings();
    } catch (error) {
      toast({
        title: "Reset failed",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      setResettingRole(null);
    }
  };

  if (userLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const disabled = !admin || isSaving;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Models</h1>
        <p className="text-muted-foreground">
          Bind each agent role to a provider and model. Changes apply org-wide.
        </p>
      </div>

      {!admin && (
        <div className="rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 px-4 py-3">
          <p className="text-sm text-amber-800 dark:text-amber-200">
            Model role bindings are admin-only. You can view current bindings but
            cannot edit them.
          </p>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Role bindings</CardTitle>
          <CardDescription>
            Each role is mapped to a (provider, model ID) pair. Use &ldquo;Reset
            to default&rdquo; to revert a single role.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : (
            bindings.map((b) => (
              <div
                key={b.role}
                className="rounded-lg border p-4 space-y-3"
              >
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-base font-semibold">
                        {ROLE_LABEL[b.role]}
                      </h3>
                      <span className="text-xs text-muted-foreground">
                        {b.is_default
                          ? "(default)"
                          : formatRelative(b.updated_at)}
                      </span>
                    </div>
                    <p className="text-sm text-muted-foreground">
                      {ROLE_DESCRIPTION[b.role]}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleReset(b.role)}
                    disabled={
                      disabled || resettingRole === b.role || b.is_default
                    }
                    title="Reset this role to the system default"
                  >
                    {resettingRole === b.role ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <>
                        <RotateCcw className="mr-2 h-4 w-4" />
                        Reset
                      </>
                    )}
                  </Button>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label htmlFor={`provider-${b.role}`}>Provider</Label>
                    <Select
                      value={b.provider}
                      onValueChange={(value) =>
                        updateRow(b.role, { provider: value })
                      }
                      disabled={disabled}
                    >
                      <SelectTrigger id={`provider-${b.role}`}>
                        <SelectValue placeholder="Select a provider" />
                      </SelectTrigger>
                      <SelectContent>
                        {PROVIDERS.map((p) => (
                          <SelectItem key={p} value={p}>
                            {p}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor={`model-${b.role}`}>Model ID</Label>
                    <Input
                      id={`model-${b.role}`}
                      value={b.model_id}
                      onChange={(e) =>
                        updateRow(b.role, { model_id: e.target.value })
                      }
                      placeholder="e.g. claude-sonnet-4.6"
                      maxLength={MODEL_ID_MAX}
                      disabled={disabled}
                    />
                  </div>
                </div>
              </div>
            ))
          )}

          <div className="flex justify-end">
            <Button onClick={handleSaveAll} disabled={disabled || isLoading}>
              {isSaving ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Saving...
                </>
              ) : (
                "Save All"
              )}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

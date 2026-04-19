"use client";

import React, { useState, useEffect, useCallback } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/use-toast";
import { useUser } from "@/hooks/useAuthHooks";
import { isAdmin } from "@/lib/roles";
import { Trash2, Plus, Terminal, ChevronRight, ChevronDown, Loader2, Lock, CheckCircle2, XCircle, Shield, ShieldCheck, ShieldX, BookOpen } from "lucide-react";
import { commandPolicyService, type CommandPolicyRule, type PolicyTemplate } from "@/lib/services/command-policies";

function RuleList({
  rules,
  onToggle,
  onDelete,
}: {
  rules: CommandPolicyRule[];
  onToggle: (rule: CommandPolicyRule) => void;
  onDelete: (id: number) => void;
}) {
  if (rules.length === 0) {
    return (
      <p className="text-xs text-muted-foreground py-4 text-center">No rules yet</p>
    );
  }
  return (
    <div className="divide-y divide-border">
      {rules.map((rule) => (
        <div
          key={rule.id}
          className={`flex items-center gap-3 px-3 py-2 transition-colors hover:bg-muted/30 ${
            !rule.enabled ? "opacity-50" : ""
          }`}
        >
          <Switch
            checked={rule.enabled}
            onCheckedChange={() => onToggle(rule)}
            className="shrink-0 scale-90"
            aria-label={`Toggle rule: ${rule.pattern}`}
          />
          <code className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-mono min-w-0 overflow-x-auto whitespace-nowrap scrollbar-thin">
            {rule.pattern}
          </code>
          <span className="text-xs text-muted-foreground truncate min-w-0 flex-1">
            {rule.description}
          </span>
          {rule.source === "template" && (
            <span className="text-[10px] text-muted-foreground/60 shrink-0">tpl</span>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 p-0 shrink-0 text-muted-foreground hover:text-destructive"
            onClick={() => onDelete(rule.id)}
            aria-label={`Delete rule: ${rule.pattern}`}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
    </div>
  );
}

function AddRuleForm({
  mode,
  onAdd,
  onCancel,
}: {
  mode: "allow" | "deny";
  onAdd: (pattern: string, description: string) => void;
  onCancel: () => void;
}) {
  const [pattern, setPattern] = useState("");
  const [desc, setDesc] = useState("");

  return (
    <div className="border-t p-3 space-y-2 bg-muted/20">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <div className="space-y-1">
          <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Pattern (regex)</Label>
          <Input
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
            placeholder={mode === "deny" ? "\\bsudo\\b" : "^ls\\b"}
            className="font-mono text-xs bg-background h-7"
          />
        </div>
        <div className="space-y-1">
          <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Description</Label>
          <Input
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder={mode === "deny" ? "Block sudo" : "Allow ls"}
            className="bg-background h-7 text-xs"
          />
        </div>
      </div>
      <div className="flex gap-2">
        <Button
          size="sm"
          className="h-6 text-xs"
          onClick={() => { if (pattern.trim()) onAdd(pattern.trim(), desc.trim()); }}
        >
          Add
        </Button>
        <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

function TemplatePicker({
  templates,
  applying,
  activeId,
  onApply,
  onRemove,
}: {
  templates: PolicyTemplate[];
  applying: string | null;
  activeId: string | null;
  onApply: (id: string) => void;
  onRemove: () => void;
}) {
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (templates.length === 0) return null;

  return (
    <div className="rounded-lg border bg-card overflow-hidden">
      <div className="flex items-center gap-2.5 px-3.5 py-2.5 border-b">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary/10 text-primary">
          <BookOpen className="h-3.5 w-3.5" />
        </div>
        <div>
          <h3 className="text-sm font-medium">Policy Templates</h3>
          <p className="text-[11px] text-muted-foreground">Pre-built security profiles for common use cases</p>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-px bg-border">
        {templates.map((tpl) => {
          const expanded = expandedId === tpl.id;
          const active = activeId === tpl.id;
          return (
            <div key={tpl.id} className="bg-card px-3.5 py-3 space-y-2">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium">
                    {tpl.name}
                  </p>
                  <p className="text-[11px] text-muted-foreground leading-relaxed mt-0.5">
                    {tpl.description}
                  </p>
                </div>
                <div className="shrink-0">
                  {active ? (
                    <div className="flex items-center gap-1.5">
                      <span className="inline-flex items-center gap-1 h-6 px-2 text-xs text-green-500">
                        <CheckCircle2 className="h-3.5 w-3.5" /> Active
                      </span>
                      <button type="button" className="text-[11px] text-muted-foreground hover:text-destructive" onClick={() => onRemove()}>
                        Remove
                      </button>
                    </div>
                  ) : confirmId === tpl.id ? (
                    <div className="flex items-center gap-1.5">
                      <Button
                        size="sm"
                        className="h-6 text-xs"
                        disabled={applying !== null}
                        onClick={() => { onApply(tpl.id); setConfirmId(null); }}
                      >
                        {applying === tpl.id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          "Confirm"
                        )}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 text-xs"
                        onClick={() => setConfirmId(null)}
                      >
                        Cancel
                      </Button>
                    </div>
                  ) : (
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-6 text-xs"
                      disabled={applying !== null}
                      onClick={() => setConfirmId(tpl.id)}
                    >
                      Apply
                    </Button>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
                  onClick={() => setExpandedId(expanded ? null : tpl.id)}
                >
                  {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                  Preview rules
                </button>
                <Badge variant="secondary" className="text-[10px] font-normal">
                  <ShieldCheck className="h-2.5 w-2.5 mr-1" />
                  {tpl.allow_count} allow
                </Badge>
                <Badge variant="secondary" className="text-[10px] font-normal">
                  <ShieldX className="h-2.5 w-2.5 mr-1" />
                  {tpl.deny_count} deny
                </Badge>
              </div>
              {expanded && (
                <div className="mt-1 space-y-1.5 text-[11px]">
                  {tpl.allow.length > 0 && (
                    <div>
                      <p className="text-muted-foreground font-medium mb-1">Allow</p>
                      {tpl.allow.map((r, i) => (
                        <div key={i} className="flex items-baseline gap-2 py-0.5">
                          <code className="shrink-0 rounded bg-primary/10 text-primary px-1 py-px font-mono text-[10px] max-w-[50%] overflow-x-auto whitespace-nowrap scrollbar-thin">
                            {r.pattern}
                          </code>
                          <span className="text-muted-foreground truncate">{r.description}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {tpl.deny.length > 0 && (
                    <div>
                      <p className="text-muted-foreground font-medium mb-1">Deny</p>
                      {tpl.deny.map((r, i) => (
                        <div key={i} className="flex items-baseline gap-2 py-0.5">
                          <code className="shrink-0 rounded bg-destructive/10 text-destructive px-1 py-px font-mono text-[10px] max-w-[50%] overflow-x-auto whitespace-nowrap scrollbar-thin">
                            {r.pattern}
                          </code>
                          <span className="text-muted-foreground truncate">{r.description}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function SecuritySettings() {
  const { user } = useUser();
  const { toast } = useToast();
  const admin = isAdmin(user?.role);

  const [allowRules, setAllowRules] = useState<CommandPolicyRule[]>([]);
  const [denyRules, setDenyRules] = useState<CommandPolicyRule[]>([]);
  const [allowlistEnabled, setAllowlistEnabled] = useState(false);
  const [denylistEnabled, setDenylistEnabled] = useState(false);
  const [loading, setLoading] = useState(true);

  const [showAddAllow, setShowAddAllow] = useState(false);
  const [showAddDeny, setShowAddDeny] = useState(false);

  const [testCmd, setTestCmd] = useState("");
  const [testResult, setTestResult] = useState<{ allowed: boolean; rule_description: string | null } | null>(null);
  const [testLoading, setTestLoading] = useState(false);

  const [templates, setTemplates] = useState<PolicyTemplate[]>([]);
  const [applyingTemplate, setApplyingTemplate] = useState<string | null>(null);
  const [activeTemplateId, setActiveTemplateId] = useState<string | null>(null);

  const fetchPolicies = useCallback(async () => {
    try {
      const data = await commandPolicyService.getPolicies();
      setAllowRules(data.allow_rules);
      setDenyRules(data.deny_rules);
      setAllowlistEnabled(data.allowlist_enabled);
      setDenylistEnabled(data.denylist_enabled);
      setActiveTemplateId(data.active_template_id ?? null);
    } catch {
      toast({ title: "Failed to load policies", variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  const fetchTemplates = useCallback(async () => {
    try {
      const data = await commandPolicyService.getTemplates();
      setTemplates(data);
    } catch {
      // Templates are optional; don't block the UI
    }
  }, []);

  useEffect(() => { fetchPolicies(); fetchTemplates(); }, [fetchPolicies, fetchTemplates]);

  const handleToggleList = async (list: "allowlist" | "denylist", enabled: boolean) => {
    try {
      const res = await commandPolicyService.toggleList(list, enabled);
      setAllowlistEnabled(res.allowlist_enabled);
      setDenylistEnabled(res.denylist_enabled);
      await fetchPolicies();
      toast({ title: `${list === "allowlist" ? "Allowlist" : "Denylist"} ${enabled ? "enabled" : "disabled"}` });
    } catch {
      toast({ title: "Failed to toggle list", variant: "destructive" });
    }
  };

  const handleAddRule = async (mode: "allow" | "deny", pattern: string, description: string) => {
    try {
      await commandPolicyService.createPolicy({ mode, pattern, description, priority: 50 });
      mode === "allow" ? setShowAddAllow(false) : setShowAddDeny(false);
      await fetchPolicies();
      toast({ title: "Rule added" });
    } catch (e) {
      toast({ title: e instanceof Error ? e.message : "Failed to add rule", variant: "destructive" });
    }
  };

  const handleToggleRule = async (rule: CommandPolicyRule) => {
    try {
      await commandPolicyService.updatePolicy(rule.id, { enabled: !rule.enabled });
      await fetchPolicies();
    } catch {
      toast({ title: "Failed to update rule", variant: "destructive" });
    }
  };

  const handleDeleteRule = async (id: number) => {
    try {
      await commandPolicyService.deletePolicy(id);
      await fetchPolicies();
    } catch {
      toast({ title: "Failed to delete rule", variant: "destructive" });
    }
  };

  const handleTest = async () => {
    if (!testCmd.trim()) return;
    setTestLoading(true);
    try {
      const result = await commandPolicyService.testCommand(testCmd.trim());
      setTestResult(result);
    } catch {
      toast({ title: "Test failed", variant: "destructive" });
    } finally {
      setTestLoading(false);
    }
  };

  const handleApplyTemplate = async (templateId: string) => {
    setApplyingTemplate(templateId);
    try {
      const res = await commandPolicyService.applyTemplate(templateId);
      setAllowlistEnabled(res.allowlist_enabled);
      setDenylistEnabled(res.denylist_enabled);
      await fetchPolicies();
      const tpl = templates.find(t => t.id === templateId);
      toast({ title: `Applied "${tpl?.name ?? templateId}" template` });
    } catch {
      toast({ title: "Failed to apply template", variant: "destructive" });
    } finally {
      setApplyingTemplate(null);
    }
  };

  const handleRemoveTemplate = async () => {
    try {
      await commandPolicyService.clearActiveTemplate();
      await fetchPolicies();
      toast({ title: "Template removed" });
    } catch {
      toast({ title: "Failed to remove template", variant: "destructive" });
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!admin) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-muted">
            <Shield className="h-4 w-4 text-muted-foreground" />
          </div>
          <div>
            <h1 className="text-sm font-semibold">Command Policies</h1>
            <p className="text-xs text-muted-foreground">Organization-level security rules</p>
          </div>
        </div>
        <Card>
          <CardContent className="py-6">
            <div className="flex flex-col items-center justify-center text-center">
              <Lock className="h-7 w-7 text-muted-foreground/50 mb-2" />
              <p className="text-xs text-muted-foreground">Requires Admin role to manage policies.</p>
              <div className="mt-3 flex items-center gap-2">
                {denylistEnabled && <Badge variant="secondary" className="text-[11px]">Denylist on</Badge>}
                {allowlistEnabled && <Badge variant="secondary" className="text-[11px]">Allowlist on</Badge>}
                {!denylistEnabled && !allowlistEnabled && (
                  <span className="text-[11px] text-muted-foreground">No active lists</span>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center gap-2.5">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/10">
          <Shield className="h-4 w-4 text-primary" />
        </div>
        <div>
          <h1 className="text-sm font-semibold">Command Policies</h1>
          <p className="text-xs text-muted-foreground">Control what commands the Aurora agent can execute</p>
        </div>
      </div>

      <p className="text-xs text-muted-foreground leading-relaxed">
        Command policies audit what the agent can execute — they do not change your connector
        permissions or read-only mode settings. A command allowed here can still fail if the
        underlying credentials lack access. If the denylist is on, matching commands are blocked.
        If the allowlist is on, non-matching commands are blocked. Enable both for maximum control.
      </p>

      {/* Policy Templates */}
      {templates.length > 0 && (
        <TemplatePicker
          templates={templates}
          applying={applyingTemplate}
          activeId={activeTemplateId}
          onApply={handleApplyTemplate}
          onRemove={handleRemoveTemplate}
        />
      )}

      {/* Denylist */}
      <div className="rounded-lg border bg-card overflow-hidden">
        <div className="flex items-center justify-between px-3.5 py-2.5 border-b">
          <div className="flex items-center gap-2.5">
            <div className={`flex h-7 w-7 items-center justify-center rounded-md ${
              denylistEnabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
            }`}>
              <ShieldX className="h-3.5 w-3.5" />
            </div>
            <div>
              <h3 className="text-sm font-medium">Denylist</h3>
              <p className="text-[11px] text-muted-foreground">
                {denylistEnabled ? "Commands matching these patterns are blocked" : "Disabled -- no commands are blocked by this list"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0 ml-3">
            {denylistEnabled && (
              <Button size="sm" variant="ghost" className="h-6 text-xs gap-1" onClick={() => setShowAddDeny(!showAddDeny)}>
                <Plus className="h-3 w-3" /> Add
              </Button>
            )}
            <Switch
              checked={denylistEnabled}
              onCheckedChange={(v) => handleToggleList("denylist", v)}
              className="scale-90"
              aria-label="Toggle denylist"
            />
          </div>
        </div>
        {denylistEnabled && (
          <>
            <RuleList rules={denyRules} onToggle={handleToggleRule} onDelete={handleDeleteRule} />
            {showAddDeny && (
              <AddRuleForm
                mode="deny"
                onAdd={(p, d) => handleAddRule("deny", p, d)}
                onCancel={() => setShowAddDeny(false)}
              />
            )}
          </>
        )}
      </div>

      {/* Allowlist */}
      <div className="rounded-lg border bg-card overflow-hidden">
        <div className="flex items-center justify-between px-3.5 py-2.5 border-b">
          <div className="flex items-center gap-2.5">
            <div className={`flex h-7 w-7 items-center justify-center rounded-md ${
              allowlistEnabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
            }`}>
              <ShieldCheck className="h-3.5 w-3.5" />
            </div>
            <div>
              <h3 className="text-sm font-medium">Allowlist</h3>
              <p className="text-[11px] text-muted-foreground">
                {allowlistEnabled ? "Only commands matching these patterns are allowed" : "Disabled -- commands are not filtered by this list"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0 ml-3">
            {allowlistEnabled && (
              <Button size="sm" variant="ghost" className="h-6 text-xs gap-1" onClick={() => setShowAddAllow(!showAddAllow)}>
                <Plus className="h-3 w-3" /> Add
              </Button>
            )}
            <Switch
              checked={allowlistEnabled}
              onCheckedChange={(v) => handleToggleList("allowlist", v)}
              className="scale-90"
              aria-label="Toggle allowlist"
            />
          </div>
        </div>
        {allowlistEnabled && (
          <>
            <RuleList rules={allowRules} onToggle={handleToggleRule} onDelete={handleDeleteRule} />
            {showAddAllow && (
              <AddRuleForm
                mode="allow"
                onAdd={(p, d) => handleAddRule("allow", p, d)}
                onCancel={() => setShowAddAllow(false)}
              />
            )}
          </>
        )}
      </div>

      {/* Test Command */}
      <div className="rounded-lg border bg-card">
        <div className="flex items-center gap-2.5 border-b px-3.5 py-2.5">
          <div className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
            <Terminal className="h-3.5 w-3.5 text-muted-foreground" />
          </div>
          <div>
            <h3 className="text-sm font-medium">Test Command</h3>
            <p className="text-[11px] text-muted-foreground">Check how a command would be evaluated</p>
          </div>
        </div>
        <div className="p-3.5 space-y-2.5">
          <div className="flex gap-2">
            <Input
              value={testCmd}
              onChange={(e) => { setTestCmd(e.target.value); setTestResult(null); }}
              placeholder="Enter a command to test..."
              className="font-mono text-xs bg-background h-8"
              onKeyDown={(e) => e.key === "Enter" && handleTest()}
            />
            <Button onClick={handleTest} disabled={testLoading || !testCmd.trim()} size="sm" className="h-8 text-xs">
              {testLoading ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <>Test<ChevronRight className="ml-1 h-3.5 w-3.5" /></>
              )}
            </Button>
          </div>
          {testResult && (
            <div className={`flex items-center gap-2.5 rounded-md border p-2.5 ${
              testResult.allowed
                ? "border-primary/30 bg-primary/5"
                : "border-muted-foreground/30 bg-muted/50"
            }`}>
              {testResult.allowed ? (
                <CheckCircle2 className="h-4 w-4 text-primary shrink-0" />
              ) : (
                <XCircle className="h-4 w-4 text-muted-foreground shrink-0" />
              )}
              <div className="min-w-0">
                <p className={`text-xs font-medium ${testResult.allowed ? "text-primary" : "text-foreground"}`}>
                  {testResult.allowed ? "Allowed" : "Denied"}
                </p>
                <p className="text-[11px] text-muted-foreground truncate">
                  {testResult.rule_description || "No matching rule"}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

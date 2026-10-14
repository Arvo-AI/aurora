"use client";

import { useEffect, useState } from "react";
import { Loader2, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export type JiraRcaFilterMode = "auto" | "manual";

export interface JiraRcaRule {
  projectKey: string;
  issueTypes: string[];
  priorities?: string[];
  labels?: string[];
  components?: string[];
  statuses?: string[];
  jql?: string;
}

interface JiraProject {
  key: string;
  name: string;
}

interface JiraAutomaticRcaProps {
  mode: JiraRcaFilterMode;
  rules: JiraRcaRule[];
  saving: boolean;
  onModeChange: (mode: JiraRcaFilterMode) => void;
  onSave: (rules: JiraRcaRule[]) => Promise<boolean>;
}

function list(values: string[] | undefined): string[] {
  return values ?? [];
}

function summarize(rule: JiraRcaRule): string {
  const parts = [rule.issueTypes.join(", ")];
  if (list(rule.priorities).length) parts.push(`priority ${list(rule.priorities).join(", ")}`);
  if (list(rule.labels).length) parts.push(`label ${list(rule.labels).join(", ")}`);
  if (list(rule.components).length) parts.push(`component ${list(rule.components).join(", ")}`);
  if (list(rule.statuses).length) parts.push(`status ${list(rule.statuses).join(", ")}`);
  if (rule.jql) parts.push(rule.jql);
  return parts.join(" · ");
}

function ChipField({
  id,
  label,
  values,
  onChange,
  disabled,
  placeholder,
}: {
  id: string;
  label: string;
  values: string[];
  onChange: (next: string[]) => void;
  disabled: boolean;
  placeholder: string;
}) {
  const [text, setText] = useState("");
  const add = () => {
    const name = text.trim().replace(/\s+/g, " ");
    if (!name) return;
    // A repeated chip is the same limit, so keep the first spelling.
    if (!values.some((item) => item.toLowerCase() === name.toLowerCase())) {
      onChange([...values, name]);
    }
    setText("");
  };
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} className="text-xs">{label}</Label>
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {values.map((value) => (
            <button
              key={value}
              type="button"
              className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs"
              onClick={() => onChange(values.filter((item) => item !== value))}
              disabled={disabled}
            >
              {value}
              <X className="h-3 w-3" />
            </button>
          ))}
        </div>
      )}
      <div className="flex items-center gap-2">
        <Input
          id={id}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={placeholder}
          className="h-8 text-sm"
          disabled={disabled}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <Button type="button" variant="outline" size="sm" className="h-8" onClick={add} disabled={disabled || !text.trim()}>
          Add
        </Button>
      </div>
    </div>
  );
}

export function JiraAutomaticRca({
  mode,
  rules,
  saving,
  onModeChange,
  onSave,
}: Readonly<JiraAutomaticRcaProps>) {
  const [projects, setProjects] = useState<JiraProject[]>([]);
  const [loadingProjects, setLoadingProjects] = useState(true);
  const [projectsError, setProjectsError] = useState(false);
  const [projectKey, setProjectKey] = useState("");
  const [typedProject, setTypedProject] = useState("");
  const [issueTypes, setIssueTypes] = useState<string[]>([]);
  const [loadingTypes, setLoadingTypes] = useState(false);
  const [typesError, setTypesError] = useState(false);
  const [checked, setChecked] = useState<string[]>([]);
  const [typedType, setTypedType] = useState("");
  const [priorities, setPriorities] = useState<string[]>([]);
  const [labels, setLabels] = useState<string[]>([]);
  const [components, setComponents] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [jql, setJql] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [fetchedKey, setFetchedKey] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoadingProjects(true);
      try {
        const res = await fetch("/api/jira/projects", { credentials: "include" });
        if (!res.ok) throw new Error("failed");
        const data = await res.json();
        if (!cancelled) setProjects(Array.isArray(data.projects) ? data.projects : []);
      } catch {
        if (!cancelled) setProjectsError(true);
      } finally {
        if (!cancelled) setLoadingProjects(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // A failed or empty project list still lets them type a key.
  const typeProjectKey = projectsError || (!loadingProjects && projects.length === 0);
  const selectedKey = (typeProjectKey ? typedProject : projectKey).trim().toUpperCase();

  useEffect(() => {
    // A dropdown pick is one value. A typed key changes on every letter, so wait.
    if (!typeProjectKey) {
      setFetchedKey(selectedKey);
      return;
    }
    const handle = setTimeout(() => setFetchedKey(selectedKey), 400);
    return () => clearTimeout(handle);
  }, [selectedKey, typeProjectKey]);

  useEffect(() => {
    if (!fetchedKey || projectsError) {
      setIssueTypes([]);
      setTypesError(false);
      return;
    }
    let cancelled = false;
    void (async () => {
      setLoadingTypes(true);
      setTypesError(false);
      try {
        const res = await fetch(
          `/api/jira/projects/${encodeURIComponent(fetchedKey)}/issue-types`,
          { credentials: "include" },
        );
        if (!res.ok) throw new Error("failed");
        const data = await res.json();
        if (!cancelled) setIssueTypes(Array.isArray(data.issueTypes) ? data.issueTypes : []);
      } catch {
        if (!cancelled) {
          setIssueTypes([]);
          setTypesError(true);
        }
      } finally {
        if (!cancelled) setLoadingTypes(false);
      }
    })();
    return () => { cancelled = true; };
  }, [fetchedKey, projectsError]);

  // Auto mode is read-only, so a half-edited manual form should not linger.
  useEffect(() => {
    if (mode !== "manual") {
      setFormOpen(false);
      setEditingIndex(null);
    }
  }, [mode]);

  const resetForm = () => {
    setFormOpen(false);
    setEditingIndex(null);
    setProjectKey("");
    setTypedProject("");
    setChecked([]);
    setTypedType("");
    setPriorities([]);
    setLabels([]);
    setComponents([]);
    setStatuses([]);
    setJql("");
  };

  const toggleType = (name: string, on: boolean) => {
    setChecked((current) => {
      const without = current.filter((item) => item.toLowerCase() !== name.toLowerCase());
      return on ? [...without, name] : without;
    });
  };

  const addTypedType = () => {
    const name = typedType.trim();
    if (!name) return;
    toggleType(name, true);
    setTypedType("");
  };

  const startAdd = () => {
    resetForm();
    setFormOpen(true);
  };

  const startEdit = (index: number) => {
    const rule = rules[index];
    setEditingIndex(index);
    setFormOpen(true);
    setProjectKey(rule.projectKey);
    setTypedProject(rule.projectKey);
    setChecked(rule.issueTypes);
    setPriorities(list(rule.priorities));
    setLabels(list(rule.labels));
    setComponents(list(rule.components));
    setStatuses(list(rule.statuses));
    setJql(rule.jql ?? "");
    setTypedType("");
  };

  const saveRule = async () => {
    if (!selectedKey || checked.length === 0) return;
    const rule: JiraRcaRule = {
      projectKey: selectedKey,
      issueTypes: checked,
      priorities,
      labels,
      components,
      statuses,
      jql: jql.trim(),
    };
    const next = [...rules];
    // Editing replaces that row. A new row can share a project with a different limit.
    if (editingIndex !== null) next[editingIndex] = rule;
    else next.push(rule);
    const ok = await onSave(next);
    if (ok) resetForm();
  };

  const removeRule = (index: number) => {
    void onSave(rules.filter((_, item) => item !== index));
    if (editingIndex === index) resetForm();
  };

  const renderProjectField = () => {
    if (loadingProjects) {
      return (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Loading projects
        </div>
      );
    }
    if (typeProjectKey) {
      return (
        <div className="space-y-1.5">
          <Label htmlFor="jira-rca-project" className="text-xs">Project key</Label>
          <Input
            id="jira-rca-project"
            value={typedProject}
            onChange={(event) => {
              setTypedProject(event.target.value);
              setChecked([]);
            }}
            placeholder="OPS"
            className="h-8 text-sm"
          />
        </div>
      );
    }
    return (
      <Select
        value={projectKey || undefined}
        onValueChange={(key) => {
          setProjectKey(key);
          setChecked([]);
        }}
        disabled={saving}
      >
        <SelectTrigger className="h-8 text-sm">
          <SelectValue placeholder="Choose a project" />
        </SelectTrigger>
        <SelectContent>
          {projects.map((project) => (
            <SelectItem key={project.key} value={project.key}>
              {project.key} — {project.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  };

  const manual = mode === "manual";

  return (
    <div className="space-y-3 mt-3">
      <div className="grid grid-cols-2 gap-2">
        {(["auto", "manual"] as const).map((value) => {
          const selected = mode === value;
          return (
            <button
              key={value}
              type="button"
              onClick={() => {
                if (!selected) onModeChange(value);
              }}
              disabled={saving}
              className={`rounded-lg border p-2.5 text-left transition-colors ${
                selected ? "border-[#2684FF] bg-[#2684FF]/[0.04]" : "border-border hover:bg-muted/50"
              }`}
            >
              <span className="text-sm font-medium">{value === "auto" ? "Auto" : "Manual"}</span>
              <p className="text-xs text-muted-foreground mt-0.5">
                {value === "auto"
                  ? "Aurora updates filters from comments on tickets"
                  : "You set every filter yourself"}
              </p>
            </button>
          );
        })}
      </div>

      <p className="text-xs text-muted-foreground">
        {manual
          ? "A ticket starts an RCA only when it matches a filter. Leave a limit empty to allow any value. Comments will not change these."
          : "A ticket starts an RCA only when it matches a filter. Aurora reads new comments on tickets and updates the filters. Switch to Manual to edit them yourself."}
      </p>

      {rules.length > 0 ? (
        <ul className="space-y-1.5">
          {rules.map((rule, index) => (
            <li key={`${rule.projectKey}-${index}`} className="flex items-start justify-between gap-2 rounded-md border px-2.5 py-2">
              {manual ? (
                <button
                  type="button"
                  className="min-w-0 text-left"
                  onClick={() => startEdit(index)}
                  disabled={saving}
                >
                  <p className="text-sm font-medium">{rule.projectKey}</p>
                  <p className="text-xs text-muted-foreground break-words">{summarize(rule)}</p>
                </button>
              ) : (
                <div className="min-w-0 text-left">
                  <p className="text-sm font-medium">{rule.projectKey}</p>
                  <p className="text-xs text-muted-foreground break-words">{summarize(rule)}</p>
                </div>
              )}
              {manual && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-muted-foreground"
                  disabled={saving}
                  onClick={() => removeRule(index)}
                  aria-label={`Remove ${rule.projectKey}`}
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">
          {manual
            ? "No filters yet. Add one, or new tickets are ignored."
            : "No filters yet. Comment on a Jira ticket, for example: only investigate Bugs in OPS at High priority."}
        </p>
      )}

      {manual && !formOpen && (
        <Button type="button" variant="outline" size="sm" className="h-8" onClick={startAdd} disabled={saving}>
          <Plus className="mr-1.5 h-3.5 w-3.5" />
          Add filter
        </Button>
      )}

      {manual && formOpen && (
        <div className="space-y-3 rounded-md border p-2.5">
          {renderProjectField()}

          {selectedKey && (
            <div className="space-y-2">
              <p className="text-xs font-medium">Ticket types</p>
              {loadingTypes ? (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Loading ticket types
                </div>
              ) : (
                [...issueTypes, ...checked.filter(
                  (name) => !issueTypes.some((known) => known.toLowerCase() === name.toLowerCase()),
                )].map((name) => {
                  const id = `jira-type-${selectedKey}-${name}`;
                  const on = checked.some((item) => item.toLowerCase() === name.toLowerCase());
                  return (
                    <label key={name} htmlFor={id} className="flex items-center gap-2 text-sm">
                      <Checkbox
                        id={id}
                        checked={on}
                        onCheckedChange={(value) => toggleType(name, value === true)}
                        disabled={saving}
                      />
                      {name}
                    </label>
                  );
                })
              )}

              {(typesError || projectsError || (!loadingTypes && issueTypes.length === 0)) && (
                <div className="flex items-center gap-2">
                  <Input
                    value={typedType}
                    onChange={(event) => setTypedType(event.target.value)}
                    placeholder="Ticket type name"
                    className="h-8 text-sm"
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addTypedType();
                      }
                    }}
                  />
                  <Button type="button" variant="outline" size="sm" className="h-8" onClick={addTypedType} disabled={!typedType.trim()}>
                    Add
                  </Button>
                </div>
              )}

              <p className="text-xs text-muted-foreground pt-1">
                Optional. A ticket matches if it has any listed value. Empty means any.
              </p>
              <ChipField id="jira-rca-priorities" label="Priorities" values={priorities} onChange={setPriorities} disabled={saving} placeholder="High" />
              <ChipField id="jira-rca-labels" label="Labels" values={labels} onChange={setLabels} disabled={saving} placeholder="production" />
              <ChipField id="jira-rca-components" label="Components" values={components} onChange={setComponents} disabled={saving} placeholder="Ticket group or component" />
              <ChipField id="jira-rca-statuses" label="Statuses" values={statuses} onChange={setStatuses} disabled={saving} placeholder="In Progress" />
              <div className="space-y-1.5">
                <Label htmlFor="jira-rca-jql" className="text-xs">Additional JQL</Label>
                <Input
                  id="jira-rca-jql"
                  value={jql}
                  onChange={(event) => setJql(event.target.value)}
                  placeholder={'"Environment" = Production'}
                  className="h-8 text-sm font-mono"
                  disabled={saving}
                />
              </div>
            </div>
          )}

          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              className="h-8"
              disabled={saving || !selectedKey || checked.length === 0}
              onClick={saveRule}
            >
              {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Plus className="mr-1.5 h-3.5 w-3.5" />}
              Save filter
            </Button>
            <Button type="button" variant="ghost" size="sm" className="h-8" onClick={resetForm} disabled={saving}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

"use client";

import { useEffect, useRef, useState } from "react";
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

function toggleIn(current: string[], name: string, on: boolean): string[] {
  const without = current.filter((item) => item.toLowerCase() !== name.toLowerCase());
  return on ? [...without, name] : without;
}

interface JiraFieldOptions {
  priorities: string[];
  labels: string[];
  components: string[];
  statuses: string[];
  labelsTruncated: boolean;
  errors: string[];
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

function OptionChecks({
  idPrefix,
  label,
  options,
  selected,
  onToggle,
  disabled,
  loading,
  failed,
  truncated,
  emptyLabel,
}: {
  idPrefix: string;
  label: string;
  options: string[];
  selected: string[];
  onToggle: (name: string, on: boolean) => void;
  disabled: boolean;
  loading: boolean;
  failed: boolean;
  truncated: boolean;
  emptyLabel: string;
}) {
  const shown = [
    ...options,
    ...selected.filter((name) => !options.some((known) => known.toLowerCase() === name.toLowerCase())),
  ];

  // A failed catalog is an error, not a free-text field. Keep values already on the rule.
  if (failed) {
    return (
      <div className="space-y-1.5">
        <p className="text-xs font-medium">{label}</p>
        <p className="text-xs text-destructive">Couldn't load {label.toLowerCase()} from Jira.</p>
        {selected.map((name) => {
          const id = `${idPrefix}-${name}`;
          return (
            <label key={name} htmlFor={id} className="flex items-center gap-2 text-sm">
              <Checkbox
                id={id}
                checked
                onCheckedChange={(value) => onToggle(name, value === true)}
                disabled={disabled}
              />
              {name}
            </label>
          );
        })}
      </div>
    );
  }

  // A loaded catalog with nothing in it.
  let choices = (
    <p className="text-xs text-muted-foreground">{emptyLabel}</p>
  );
  // Catalog request is still in flight.
  if (loading) {
    choices = (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Loading {label.toLowerCase()}
      </div>
    );
  } else if (shown.length > 0) {
    choices = (
      <div className="max-h-40 space-y-2 overflow-y-auto">
        {shown.map((name) => {
          const id = `${idPrefix}-${name}`;
          const on = selected.some((item) => item.toLowerCase() === name.toLowerCase());
          return (
            <label key={name} htmlFor={id} className="flex items-center gap-2 text-sm">
              <Checkbox
                id={id}
                checked={on}
                onCheckedChange={(value) => onToggle(name, value === true)}
                disabled={disabled}
              />
              {name}
            </label>
          );
        })}
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium">{label}</p>
      {choices}
      {truncated && !loading && (
        <p className="text-xs text-muted-foreground">Showing the first 100 labels.</p>
      )}
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
  const [loadingProjects, setLoadingProjects] = useState(false);
  const [projectsError, setProjectsError] = useState(false);
  const projectsFetchStarted = useRef(false);
  const [projectKey, setProjectKey] = useState("");
  const [lookupKey, setLookupKey] = useState("");
  const immediateLookup = useRef(false);
  const [issueTypes, setIssueTypes] = useState<string[]>([]);
  const [loadingTypes, setLoadingTypes] = useState(false);
  const [typesError, setTypesError] = useState(false);
  const [checked, setChecked] = useState<string[]>([]);
  const [priorities, setPriorities] = useState<string[]>([]);
  const [labels, setLabels] = useState<string[]>([]);
  const [components, setComponents] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [fieldOptions, setFieldOptions] = useState<JiraFieldOptions | null>(null);
  const [loadedOptionsKey, setLoadedOptionsKey] = useState("");
  const [optionsErrorKey, setOptionsErrorKey] = useState("");
  const [jql, setJql] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  useEffect(() => {
    // The form is the only place that lists projects, including in auto where it stays closed.
    if (!formOpen || projectsFetchStarted.current) return;
    projectsFetchStarted.current = true;
    setLoadingProjects(true);
    void (async () => {
      try {
        const res = await fetch("/api/jira/projects", { credentials: "include" });
        if (!res.ok) throw new Error("failed");
        const data = await res.json();
        setProjects(Array.isArray(data.projects) ? data.projects : []);
      } catch {
        setProjectsError(true);
      } finally {
        setLoadingProjects(false);
      }
    })();
  }, [formOpen]);

  useEffect(() => {
    // A dropdown pick already set the lookup key. Typing waits so O, OP, OPS are not three requests.
    if (immediateLookup.current) {
      immediateLookup.current = false;
      setLookupKey(projectKey.trim());
      return;
    }
    const handle = window.setTimeout(() => setLookupKey(projectKey.trim()), 400);
    return () => window.clearTimeout(handle);
  }, [projectKey]);

  useEffect(() => {
    if (!lookupKey) {
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
          `/api/jira/projects/${encodeURIComponent(lookupKey)}/issue-types`,
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
  }, [lookupKey]);

  useEffect(() => {
    if (!lookupKey) {
      setFieldOptions(null);
      setLoadedOptionsKey("");
      setOptionsErrorKey("");
      return;
    }
    let cancelled = false;
    void (async () => {
      setLoadedOptionsKey("");
      setOptionsErrorKey("");
      try {
        const res = await fetch(
          `/api/jira/projects/${encodeURIComponent(lookupKey)}/field-options`,
          { credentials: "include" },
        );
        if (!res.ok) throw new Error("failed");
        const data = await res.json();
        if (cancelled) return;
        setFieldOptions({
          priorities: Array.isArray(data.priorities) ? data.priorities : [],
          labels: Array.isArray(data.labels) ? data.labels : [],
          components: Array.isArray(data.components) ? data.components : [],
          statuses: Array.isArray(data.statuses) ? data.statuses : [],
          labelsTruncated: data.labelsTruncated === true,
          errors: Array.isArray(data.errors) ? data.errors : [],
        });
        setLoadedOptionsKey(lookupKey);
      } catch {
        if (!cancelled) {
          setFieldOptions(null);
          setOptionsErrorKey(lookupKey);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [lookupKey]);

  // Auto mode is read-only, so a half-edited manual form should not linger.
  useEffect(() => {
    if (mode !== "manual") {
      setFormOpen(false);
      setEditingIndex(null);
    }
  }, [mode]);

  const clearProjectChoices = () => {
    setChecked([]);
    setPriorities([]);
    setLabels([]);
    setComponents([]);
    setStatuses([]);
  };

  const resetForm = () => {
    setFormOpen(false);
    setEditingIndex(null);
    setProjectKey("");
    setChecked([]);
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

  const startAdd = () => {
    resetForm();
    setFormOpen(true);
  };

  const startEdit = (index: number) => {
    const rule = rules[index];
    setEditingIndex(index);
    setFormOpen(true);
    // Editing a saved project should load its types now, not after the typing delay.
    immediateLookup.current = true;
    setProjectKey(rule.projectKey);
    setChecked(rule.issueTypes);
    setPriorities(list(rule.priorities));
    setLabels(list(rule.labels));
    setComponents(list(rule.components));
    setStatuses(list(rule.statuses));
    setJql(rule.jql ?? "");
  };

  const saveRule = async () => {
    const key = projectKey.trim();
    if (!key || checked.length === 0) return;
    const rule: JiraRcaRule = {
      projectKey: key,
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
    const listed = projects.some((project) => project.key === projectKey);
    return (
      <div className="space-y-1.5">
        <Label htmlFor="jira-rca-project" className="text-xs">Project key</Label>
        <Input
          id="jira-rca-project"
          value={projectKey}
          onChange={(event) => {
            setProjectKey(event.target.value);
            clearProjectChoices();
          }}
          placeholder="OPS"
          className="h-8 text-sm"
          disabled={saving}
        />
        {loadingProjects ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Loading projects
          </div>
        ) : projects.length > 0 ? (
          <Select
            value={listed ? projectKey : undefined}
            onValueChange={(key) => {
              immediateLookup.current = true;
              setProjectKey(key);
              clearProjectChoices();
            }}
            disabled={saving}
          >
            <SelectTrigger className="h-8 text-sm">
              <SelectValue placeholder="Or choose a listed project" />
            </SelectTrigger>
            <SelectContent>
              {projects.map((project) => (
                <SelectItem key={project.key} value={project.key}>
                  {project.key} — {project.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : projectsError ? (
          <p className="text-xs text-muted-foreground">Couldn&apos;t load the project list. Type a project key.</p>
        ) : (
          <p className="text-xs text-muted-foreground">No Jira projects found. Type a project key.</p>
        )}
      </div>
    );
  };

  const optionsLoading = Boolean(lookupKey) && loadedOptionsKey !== lookupKey && optionsErrorKey !== lookupKey;
  const optionsFailed = Boolean(lookupKey) && optionsErrorKey === lookupKey;
  const fieldFailed = (name: string) => fieldOptions?.errors.includes(name) === true && loadedOptionsKey === lookupKey;

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
                  ? "Aurora updates existing filters from comments on those projects"
                  : "You set every filter yourself"}
              </p>
            </button>
          );
        })}
      </div>

      <p className="text-xs text-muted-foreground">
        {manual
          ? "A ticket starts an RCA only when it matches a filter. Leave a limit empty to allow any value. Comments will not change these."
          : "A ticket starts an RCA only when it matches a filter. Comments on a project that already has a filter can update it. Switch to Manual to edit filters yourself."}
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
            : "No filters yet. Switch to Manual to add the first one. After that, comments on those projects can update it."}
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

          {projectKey.trim() && (
            <div className="space-y-2">
              <p className="text-xs font-medium">Ticket types</p>
              {loadingTypes || projectKey.trim() !== lookupKey ? (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Loading ticket types
                </div>
              ) : typesError ? (
                <div className="space-y-2">
                  <p className="text-xs text-destructive">Couldn't load ticket types from Jira.</p>
                  {checked.map((name) => {
                    const id = `jira-type-${projectKey}-${name}`;
                    return (
                      <label key={name} htmlFor={id} className="flex items-center gap-2 text-sm">
                        <Checkbox
                          id={id}
                          checked
                          onCheckedChange={(value) => toggleType(name, value === true)}
                          disabled={saving}
                        />
                        {name}
                      </label>
                    );
                  })}
                </div>
              ) : issueTypes.length === 0 ? (
                <p className="text-xs text-muted-foreground">No ticket types found in this project.</p>
              ) : (
                [...issueTypes, ...checked.filter(
                  (name) => !issueTypes.some((known) => known.toLowerCase() === name.toLowerCase()),
                )].map((name) => {
                  const id = `jira-type-${projectKey}-${name}`;
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

              <p className="text-xs text-muted-foreground pt-1">
                Optional. A ticket matches if it has any listed value. Empty means any.
              </p>
              <OptionChecks
                idPrefix="jira-rca-priorities"
                label="Priorities"
                options={fieldOptions?.priorities ?? []}
                selected={priorities}
                onToggle={(name, on) => setPriorities((current) => toggleIn(current, name, on))}
                disabled={saving}
                loading={optionsLoading}
                failed={optionsFailed || fieldFailed("priorities")}
                truncated={false}
                emptyLabel="No priorities found in Jira."
              />
              <OptionChecks
                idPrefix="jira-rca-labels"
                label="Labels"
                options={fieldOptions?.labels ?? []}
                selected={labels}
                onToggle={(name, on) => setLabels((current) => toggleIn(current, name, on))}
                disabled={saving}
                loading={optionsLoading}
                failed={optionsFailed || fieldFailed("labels")}
                truncated={fieldOptions?.labelsTruncated === true && !optionsLoading && !optionsFailed}
                emptyLabel="No labels yet."
              />
              <OptionChecks
                idPrefix="jira-rca-components"
                label="Components"
                options={fieldOptions?.components ?? []}
                selected={components}
                onToggle={(name, on) => setComponents((current) => toggleIn(current, name, on))}
                disabled={saving}
                loading={optionsLoading}
                failed={optionsFailed || fieldFailed("components")}
                truncated={false}
                emptyLabel="This project has no components."
              />
              <OptionChecks
                idPrefix="jira-rca-statuses"
                label="Statuses"
                options={fieldOptions?.statuses ?? []}
                selected={statuses}
                onToggle={(name, on) => setStatuses((current) => toggleIn(current, name, on))}
                disabled={saving}
                loading={optionsLoading}
                failed={optionsFailed || fieldFailed("statuses")}
                truncated={false}
                emptyLabel="No statuses found in this project."
              />
              <div className="space-y-1.5">
                <Label htmlFor="jira-rca-jql" className="text-xs">
                  Additional JQL <span className="font-normal text-muted-foreground">(optional)</span>
                </Label>
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
              disabled={saving || !projectKey.trim() || checked.length === 0}
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

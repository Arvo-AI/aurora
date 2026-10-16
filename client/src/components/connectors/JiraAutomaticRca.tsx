"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

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
  rules: JiraRcaRule[];
  saving: boolean;
  onSave: (rules: JiraRcaRule[]) => Promise<boolean>;
}

// Every picker on a rule. Ticket types are required; the rest only narrow the match.
const PICKERS = [
  { field: "issueTypes", label: "Ticket types", noun: "", empty: "No ticket types found in this project." },
  { field: "priorities", label: "Priorities", noun: "priority", empty: "No priorities found in Jira." },
  { field: "labels", label: "Labels", noun: "label", empty: "No labels yet." },
  { field: "components", label: "Components", noun: "component", empty: "This project has no components." },
  { field: "statuses", label: "Statuses", noun: "status", empty: "No statuses found in this project." },
] as const;

type PickField = (typeof PICKERS)[number]["field"];
type Picks = Record<PickField, string[]>;

interface FieldCatalog extends Picks {
  labelsTruncated: boolean;
  errors: string[];
}

const noPicks = (): Picks => ({ issueTypes: [], priorities: [], labels: [], components: [], statuses: [] });

const sameName = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();
const hasName = (names: string[], name: string) => names.some((item) => sameName(item, name));

function toggleIn(current: string[], name: string, on: boolean): string[] {
  const without = current.filter((item) => !sameName(item, name));
  return on ? [...without, name] : without;
}

function summarize(rule: JiraRcaRule): string {
  const parts = [rule.issueTypes.join(", ")];
  for (const { field, noun } of PICKERS.slice(1)) {
    const values = rule[field] ?? [];
    if (values.length) parts.push(`${noun} ${values.join(", ")}`);
  }
  if (rule.jql) parts.push(rule.jql);
  return parts.join(" · ");
}

function OptionChecks({
  idPrefix,
  label,
  options,
  selected,
  onChange,
  disabled,
  loading,
  failed,
  truncated,
  emptyLabel,
}: Readonly<{
  idPrefix: string;
  label: string;
  options: string[];
  selected: string[];
  onChange: (names: string[]) => void;
  disabled: boolean;
  loading: boolean;
  failed: boolean;
  truncated: boolean;
  emptyLabel: string;
}>) {
  // A failed catalog is an error, not a free-text field. Keep values already on the rule.
  const shown = failed
    ? selected
    : [...options, ...selected.filter((name) => !hasName(options, name))];
  const everyOn = shown.length > 0 && shown.every((name) => hasName(selected, name));
  const pending = loading && !failed;

  let choices: ReactNode = null;
  // Catalog request is still in flight.
  if (pending) {
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
          return (
            <label key={name} htmlFor={id} className="flex items-center gap-2 text-sm">
              <Checkbox
                id={id}
                checked={hasName(selected, name)}
                onCheckedChange={(value) => onChange(toggleIn(selected, name, value === true))}
                disabled={disabled}
              />
              {name}
            </label>
          );
        })}
      </div>
    );
  } else if (!failed) {
    // A loaded catalog with nothing in it.
    choices = <p className="text-xs text-muted-foreground">{emptyLabel}</p>;
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium">{label}</p>
        {!pending && !failed && shown.length > 0 && (
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
            disabled={disabled}
            onClick={() => onChange(everyOn ? [] : shown)}
          >
            {everyOn ? "Clear" : "Select all"}
          </button>
        )}
      </div>
      {failed && <p className="text-xs text-destructive">Couldn&apos;t load {label.toLowerCase()} from Jira.</p>}
      {choices}
      {truncated && !pending && (
        <p className="text-xs text-muted-foreground">Showing the first 100 labels.</p>
      )}
    </div>
  );
}

export function JiraAutomaticRca({
  rules,
  saving,
  onSave,
}: Readonly<JiraAutomaticRcaProps>) {
  const [projects, setProjects] = useState<JiraProject[]>([]);
  const [loadingProjects, setLoadingProjects] = useState(false);
  const [projectsSettled, setProjectsSettled] = useState(false);
  const [projectsError, setProjectsError] = useState(false);
  const projectsFetchStarted = useRef(false);
  const [projectKey, setProjectKey] = useState("");
  const [picks, setPicks] = useState<Picks>(noPicks);
  const [catalog, setCatalog] = useState<FieldCatalog | null>(null);
  const [loadedKey, setLoadedKey] = useState("");
  const [errorKey, setErrorKey] = useState("");
  const [jql, setJql] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  useEffect(() => {
    // The form is the only place that lists projects.
    if (!formOpen || projectsFetchStarted.current) return;
    projectsFetchStarted.current = true;
    setLoadingProjects(true);
    setProjectsError(false);
    void (async () => {
      try {
        const res = await fetch("/api/jira/projects", { credentials: "include" });
        if (!res.ok) throw new Error("failed");
        const data = await res.json();
        setProjects(Array.isArray(data.projects) ? data.projects : []);
      } catch {
        setProjectsError(true);
        // Closing and reopening the form should try the project list again.
        projectsFetchStarted.current = false;
      } finally {
        setLoadingProjects(false);
        setProjectsSettled(true);
      }
    })();
  }, [formOpen]);

  useEffect(() => {
    const key = projectKey.trim();
    setLoadedKey("");
    setErrorKey("");
    // No project chosen yet, so there is nothing to list.
    if (!key) {
      setCatalog(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(
          `/api/jira/projects/${encodeURIComponent(key)}/field-options`,
          { credentials: "include" },
        );
        if (!res.ok) throw new Error("failed");
        const data = await res.json();
        if (cancelled) return;
        const names = (value: unknown) => (Array.isArray(value) ? value : []);
        setCatalog({
          issueTypes: names(data.issueTypes),
          priorities: names(data.priorities),
          labels: names(data.labels),
          components: names(data.components),
          statuses: names(data.statuses),
          labelsTruncated: data.labelsTruncated === true,
          errors: names(data.errors),
        });
        setLoadedKey(key);
      } catch {
        if (!cancelled) {
          setCatalog(null);
          setErrorKey(key);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [projectKey]);

  const resetForm = () => {
    setFormOpen(false);
    setEditingIndex(null);
    setProjectKey("");
    setPicks(noPicks());
    setJql("");
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
    setPicks({
      issueTypes: rule.issueTypes,
      priorities: rule.priorities ?? [],
      labels: rule.labels ?? [],
      components: rule.components ?? [],
      statuses: rule.statuses ?? [],
    });
    setJql(rule.jql ?? "");
  };

  const saveRule = async () => {
    const key = projectKey.trim();
    if (!key || picks.issueTypes.length === 0) return;
    const rule: JiraRcaRule = { projectKey: key, ...picks, jql: jql.trim() };
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
    // Settled stays false until the request finishes, so an empty list is not a flash of "none found".
    if (loadingProjects || !projectsSettled) {
      return (
        <div className="space-y-1.5">
          <Label className="text-xs">Project</Label>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Loading projects
          </div>
        </div>
      );
    }

    // A failed or empty catalog is an error, not a free-text project key.
    if (projectsError || projects.length === 0) {
      return (
        <div className="space-y-1.5">
          <Label className="text-xs">Project</Label>
          <p className="text-xs text-destructive">
            {projectsError ? "Couldn't load projects from Jira." : "No Jira projects found."}
          </p>
          {projectKey && <p className="text-sm">{projectKey}</p>}
        </div>
      );
    }

    // Keep a saved key visible when Jira no longer lists that project.
    const listed = projects.some((project) => project.key === projectKey);
    const options = listed || !projectKey ? projects : [...projects, { key: projectKey, name: "" }];

    return (
      <div className="space-y-1.5">
        <Label htmlFor="jira-rca-project" className="text-xs">Project</Label>
        <Select
          value={projectKey || undefined}
          onValueChange={(key) => {
            setProjectKey(key);
            setPicks(noPicks());
          }}
          disabled={saving}
        >
          <SelectTrigger id="jira-rca-project" className="h-8 text-sm">
            <SelectValue placeholder="Choose a project" />
          </SelectTrigger>
          <SelectContent>
            {options.map((project) => (
              <SelectItem key={project.key} value={project.key}>
                {project.name ? `${project.key} — ${project.name}` : project.key}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    );
  };

  const selectedKey = projectKey.trim();
  const catalogLoading = Boolean(selectedKey) && loadedKey !== selectedKey && errorKey !== selectedKey;
  const catalogFailed = Boolean(selectedKey) && errorKey === selectedKey;

  const renderPicker = ({ field, label, empty }: (typeof PICKERS)[number]) => (
    <OptionChecks
      key={field}
      idPrefix={`jira-rca-${field}-${selectedKey}`}
      label={label}
      options={catalog?.[field] ?? []}
      selected={picks[field]}
      onChange={(names) => setPicks((current) => ({ ...current, [field]: names }))}
      disabled={saving}
      loading={catalogLoading}
      failed={catalogFailed || (loadedKey === selectedKey && catalog?.errors.includes(field) === true)}
      truncated={field === "labels" && catalog?.labelsTruncated === true && !catalogLoading && !catalogFailed}
      emptyLabel={empty}
    />
  );

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-[#2684FF]/30 bg-[#2684FF]/[0.06] px-2.5 py-2">
        <p className="text-sm font-medium">Matching tickets start an RCA</p>
        <p className="text-xs text-muted-foreground mt-0.5">
          Without any filters, no ticket triggers an RCA. A ticket that matches a filter is investigated. A ticket that doesn&apos;t match is ignored.
        </p>
      </div>

      {rules.length > 0 ? (
        <ul className="space-y-1.5">
          {rules.map((rule, index) => (
            <li key={`${rule.projectKey}-${index}`} className="flex items-start justify-between gap-2 rounded-md border px-2.5 py-2">
              <button
                type="button"
                className="min-w-0 text-left"
                onClick={() => startEdit(index)}
                disabled={saving}
              >
                <p className="text-sm font-medium">{rule.projectKey}</p>
                <p className="text-xs text-muted-foreground break-words">{summarize(rule)}</p>
              </button>
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
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">No filters yet.</p>
      )}

      {!formOpen && (
        <Button type="button" variant="outline" size="sm" className="h-8" onClick={startAdd} disabled={saving}>
          <Plus className="mr-1.5 h-3.5 w-3.5" />
          Add filter
        </Button>
      )}

      {formOpen && (
        <div className="space-y-3 rounded-md border p-2.5">
          {renderProjectField()}

          {selectedKey && (
            <div className="space-y-2">
              {renderPicker(PICKERS[0])}
              <p className="text-xs text-muted-foreground pt-1">
                Optional. A ticket matches if it has any listed value. Empty means any.
              </p>
              {PICKERS.slice(1).map(renderPicker)}
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
              disabled={saving || !selectedKey || picks.issueTypes.length === 0}
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

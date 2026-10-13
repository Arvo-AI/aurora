"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export interface JiraRcaRule {
  projectKey: string;
  issueTypes: string[];
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

export function JiraAutomaticRca({ rules, saving, onSave }: Readonly<JiraAutomaticRcaProps>) {
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
  const rulesRef = useRef(rules);
  rulesRef.current = rules;

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
    if (!selectedKey || projectsError) {
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
          `/api/jira/projects/${encodeURIComponent(selectedKey)}/issue-types`,
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
  }, [selectedKey, projectsError]);

  useEffect(() => {
    // Switching projects reloads that project's saved types. Edits after that stay local.
    const existing = rulesRef.current.find((rule) => rule.projectKey.toUpperCase() === selectedKey);
    setChecked(existing?.issueTypes ?? []);
    setTypedType("");
  }, [selectedKey]);

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

  const saveRule = async () => {
    if (!selectedKey || checked.length === 0) return;
    const next = rules.filter((rule) => rule.projectKey.toUpperCase() !== selectedKey);
    next.push({ projectKey: selectedKey, issueTypes: checked });
    const ok = await onSave(next);
    if (ok) {
      setProjectKey("");
      setTypedProject("");
      setChecked([]);
    }
  };

  const removeRule = (key: string) => {
    void onSave(rules.filter((rule) => rule.projectKey !== key));
  };

  const renderProjectField = () => {
    // Projects are still loading, so there is nothing to pick yet.
    if (loadingProjects) {
      return (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Loading projects
        </div>
      );
    }
    // The project list failed or came back empty, so they type a key.
    if (typeProjectKey) {
      return (
        <div className="space-y-1.5">
          <Label htmlFor="jira-rca-project" className="text-xs">Project key</Label>
          <Input
            id="jira-rca-project"
            value={typedProject}
            onChange={(event) => setTypedProject(event.target.value)}
            placeholder="OPS"
            className="h-8 text-sm"
          />
        </div>
      );
    }
    return (
      <Select value={projectKey || undefined} onValueChange={setProjectKey} disabled={saving}>
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

  return (
    <div className="space-y-3 mt-3">
      <div className="space-y-1">
        <p className="text-sm font-medium">Which tickets start an RCA</p>
        <p className="text-xs text-muted-foreground">
          Automatic investigation runs only for a project and ticket type you add here.
          Until you add one, webhooks are ignored. Investigating a ticket yourself still works,
          and commenting back is the switch above.
        </p>
      </div>

      {rules.length > 0 && (
        <ul className="space-y-1.5">
          {rules.map((rule) => (
            <li key={rule.projectKey} className="flex items-start justify-between gap-2 rounded-md border px-2.5 py-2">
              <div className="min-w-0">
                <p className="text-sm font-medium">{rule.projectKey}</p>
                <p className="text-xs text-muted-foreground">{rule.issueTypes.join(", ")}</p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 px-2 text-muted-foreground"
                disabled={saving}
                onClick={() => removeRule(rule.projectKey)}
                aria-label={`Remove ${rule.projectKey}`}
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      {renderProjectField()}

      {selectedKey && (
        <div className="space-y-2 rounded-md border p-2.5">
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

          {checked.length > 0 && (typesError || projectsError) && (
            <p className="text-xs text-muted-foreground">{checked.join(", ")}</p>
          )}

          <Button
            type="button"
            size="sm"
            className="h-8"
            disabled={saving || checked.length === 0}
            onClick={saveRule}
          >
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Plus className="mr-1.5 h-3.5 w-3.5" />}
            Save rule
          </Button>
        </div>
      )}
    </div>
  );
}

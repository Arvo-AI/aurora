"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  serviceNowService,
  type ServiceNowChoice,
  type ServiceNowFieldOptions,
  type ServiceNowRcaRule,
} from "@/lib/services/servicenow";

interface ServiceNowAutomaticRcaProps {
  rules: ServiceNowRcaRule[];
  saving: boolean;
  onSave: (rules: ServiceNowRcaRule[]) => Promise<boolean>;
}

// Every picker on a rule. All are optional; a rule needs at least one value or a query.
const PICKERS = [
  { field: "priorities", label: "Priorities", noun: "priority", empty: "No priorities found." },
  { field: "assignmentGroups", label: "Assignment groups", noun: "group", empty: "No assignment groups found on this instance." },
  { field: "categories", label: "Categories", noun: "category", empty: "No incident categories found on this instance." },
  { field: "states", label: "States", noun: "state", empty: "No states found." },
] as const;

type PickField = (typeof PICKERS)[number]["field"];
type Picks = Record<PickField, string[]>;

const noPicks = (): Picks => ({ priorities: [], assignmentGroups: [], categories: [], states: [] });

const sameName = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();
const hasName = (names: string[], name: string) => names.some((item) => sameName(item, name));

function toggleIn(current: string[], name: string, on: boolean): string[] {
  const without = current.filter((item) => !sameName(item, name));
  return on ? [...without, name] : without;
}

function asChoices(values: ReadonlyArray<string | ServiceNowChoice>): ServiceNowChoice[] {
  return values.map((item) => (typeof item === "string" ? { value: item, label: item } : item));
}

function labelFor(options: ServiceNowChoice[], value: string): string {
  return options.find((option) => sameName(option.value, value))?.label ?? value;
}

export function summarizeRule(rule: ServiceNowRcaRule, options: ServiceNowFieldOptions | null): string {
  const parts: string[] = [];
  for (const { field, noun } of PICKERS) {
    const values = rule[field] ?? [];
    if (!values.length) continue;
    const choices = options ? asChoices(options[field]) : [];
    parts.push(`${noun} ${values.map((value) => labelFor(choices, value)).join(", ")}`);
  }
  if (rule.query) parts.push(rule.query);
  return parts.join(" · ") || "every incident";
}

// Mirrors MAX_VALUES in server/utils/rca_rule_lists.py: a rule lists at most this many names per field.
const MAX_RULE_VALUES = 30;

function OptionChecks({
  idPrefix,
  label,
  options,
  selected,
  onChange,
  disabled,
  loading,
  failed,
  emptyLabel,
  max,
}: Readonly<{
  idPrefix: string;
  label: string;
  options: ServiceNowChoice[];
  selected: string[];
  onChange: (values: string[]) => void;
  disabled: boolean;
  loading: boolean;
  failed: boolean;
  emptyLabel: string;
  max: number;
}>) {
  // A failed catalog is an error, not a free-text field. Keep values already on the rule.
  const known = options.map((option) => option.value);
  const shown: ServiceNowChoice[] = failed
    ? selected.map((value) => ({ value, label: value }))
    : [...options, ...selected.filter((value) => !hasName(known, value)).map((value) => ({ value, label: value }))];
  const everyOn = shown.length > 0 && shown.every((option) => hasName(selected, option.value));
  const pending = loading && !failed;
  const atCap = selected.length >= max;
  const tooManyToSelectAll = shown.length > max;

  let choices: ReactNode = null;
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
        {shown.map((option) => {
          const id = `${idPrefix}-${option.value}`;
          const checked = hasName(selected, option.value);
          return (
            <label key={option.value} htmlFor={id} className="flex items-center gap-2 text-sm">
              <Checkbox
                id={id}
                checked={checked}
                onCheckedChange={(value) => onChange(toggleIn(selected, option.value, value === true))}
                disabled={disabled || (!checked && atCap)}
              />
              {option.label}
            </label>
          );
        })}
      </div>
    );
  } else if (!failed) {
    choices = <p className="text-xs text-muted-foreground">{emptyLabel}</p>;
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium">{label}</p>
        {!pending && !failed && shown.length > 0 && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {tooManyToSelectAll && (
              <span>
                {selected.length}/{max} (leave empty to match any)
              </span>
            )}
            {(!tooManyToSelectAll || selected.length > 0) && (
              <button
                type="button"
                className="hover:text-foreground disabled:opacity-50"
                disabled={disabled}
                onClick={() => onChange(everyOn || tooManyToSelectAll ? [] : shown.map((option) => option.value))}
              >
                {everyOn || tooManyToSelectAll ? "Clear" : "Select all"}
              </button>
            )}
          </div>
        )}
      </div>
      {failed && <p className="text-xs text-destructive">Couldn&apos;t load {label.toLowerCase()} from ServiceNow.</p>}
      {choices}
    </div>
  );
}

export function ServiceNowAutomaticRca({ rules, saving, onSave }: Readonly<ServiceNowAutomaticRcaProps>) {
  const [options, setOptions] = useState<ServiceNowFieldOptions | null>(null);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [optionsFailed, setOptionsFailed] = useState(false);
  const optionsFetchStarted = useRef(false);
  const [picks, setPicks] = useState<Picks>(noPicks);
  const [query, setQuery] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  const loadOptions = async () => {
    optionsFetchStarted.current = true;
    setOptionsLoading(true);
    setOptionsFailed(false);
    try {
      setOptions(await serviceNowService.getFieldOptions());
    } catch {
      setOptionsFailed(true);
      // Reopening the form should try the catalog again.
      optionsFetchStarted.current = false;
    } finally {
      setOptionsLoading(false);
    }
  };

  useEffect(() => {
    // The catalog is read from the instance only when someone opens the form or has saved rules to label.
    if ((!formOpen && rules.length === 0) || optionsFetchStarted.current) return;
    void loadOptions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [formOpen, rules.length]);

  const resetForm = () => {
    setFormOpen(false);
    setEditingIndex(null);
    setPicks(noPicks());
    setQuery("");
  };

  const startAdd = () => {
    resetForm();
    setFormOpen(true);
  };

  const startEdit = (index: number) => {
    const rule = rules[index];
    setEditingIndex(index);
    setFormOpen(true);
    setPicks({
      priorities: rule.priorities ?? [],
      assignmentGroups: rule.assignmentGroups ?? [],
      categories: rule.categories ?? [],
      states: rule.states ?? [],
    });
    setQuery(rule.query ?? "");
  };

  const hasAnyPick = PICKERS.some(({ field }) => picks[field].length > 0) || query.trim().length > 0;

  const saveRule = async () => {
    if (!hasAnyPick) return;
    const rule: ServiceNowRcaRule = { ...picks, query: query.trim() };
    const next = [...rules];
    if (editingIndex !== null) next[editingIndex] = rule;
    else next.push(rule);
    const ok = await onSave(next);
    if (ok) resetForm();
  };

  const removeRule = async (index: number) => {
    const ok = await onSave(rules.filter((_, item) => item !== index));
    if (!ok) return;
    if (editingIndex === index) resetForm();
    else if (editingIndex !== null && editingIndex > index) setEditingIndex(editingIndex - 1);
  };

  const renderPicker = ({ field, label, empty }: (typeof PICKERS)[number]) => (
    <OptionChecks
      key={field}
      idPrefix={`servicenow-rca-${field}`}
      label={label}
      options={options ? asChoices(options[field]) : []}
      selected={picks[field]}
      onChange={(values) => setPicks((current) => ({ ...current, [field]: values }))}
      disabled={saving}
      loading={optionsLoading}
      failed={optionsFailed || options?.errors.includes(field) === true}
      emptyLabel={empty}
      max={MAX_RULE_VALUES}
    />
  );

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-[#62D84E]/40 bg-[#62D84E]/[0.08] px-2.5 py-2">
        <p className="text-sm font-medium">Matching incidents start an RCA</p>
        <p className="text-xs text-muted-foreground mt-0.5">
          Without any filters, no incident triggers an RCA. A new incident that matches a filter is investigated. One that doesn&apos;t match is ignored and never becomes an Aurora incident.
        </p>
      </div>

      {rules.length > 0 ? (
        <ul className="space-y-1.5">
          {rules.map((rule, index) => (
            <li key={JSON.stringify(rule)} className="flex items-start justify-between gap-2 rounded-md border px-2.5 py-2">
              <button type="button" className="min-w-0 text-left" onClick={() => startEdit(index)} disabled={saving}>
                <p className="text-sm font-medium">Filter {index + 1}</p>
                <p className="text-xs text-muted-foreground break-words">{summarizeRule(rule, options)}</p>
              </button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 px-2 text-muted-foreground"
                disabled={saving}
                onClick={() => void removeRule(index)}
                aria-label={`Remove filter ${index + 1}`}
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
          <p className="text-xs text-muted-foreground">
            An incident matches if it has any listed value in every picked dimension. Empty means any.
          </p>
          {optionsFailed && (
            <div className="flex items-center gap-2">
              <p className="text-xs text-destructive">Couldn&apos;t load the option lists from ServiceNow.</p>
              <Button type="button" variant="outline" size="sm" className="h-7" onClick={() => void loadOptions()}>
                Retry
              </Button>
            </div>
          )}
          <div className="space-y-2">{PICKERS.map(renderPicker)}</div>
          <div className="space-y-1.5">
            <Label htmlFor="servicenow-rca-query" className="text-xs">
              Additional encoded query <span className="font-normal text-muted-foreground">(optional)</span>
            </Label>
            <Input
              id="servicenow-rca-query"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="cmdb_ci.name=payments-api^impact=1"
              className="h-8 text-sm font-mono"
              disabled={saving}
            />
            <p className="text-xs text-muted-foreground">
              Checked against the instance before the RCA starts, as <code>sys_id=&lt;incident&gt;^&lt;query&gt;</code>.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Button type="button" size="sm" className="h-8" disabled={saving || !hasAnyPick} onClick={saveRule}>
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

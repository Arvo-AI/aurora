'use client';

import * as React from 'react';
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronsUpDown,
  ExternalLink,
  Loader2,
  Search,
} from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { postmortemService } from '@/lib/services/incidents';

// -----------------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------------

interface NotionDatabaseSummary {
  id: string;
  title: string;
  url?: string;
  icon?: { emoji?: string } | string | null;
  description?: string;
}

interface NotionDatabaseDetail {
  id: string;
  title: string;
  titleProperty: string | null;
  properties: Record<string, { type: string; id: string }>;
}

interface ExportResult {
  pageUrl: string;
  pageId: string;
  actionItemCount?: number;
}

interface ExportToNotionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  incidentId: string;
  onExported: (result: { pageUrl: string; pageId: string }) => void;
}

const SOURCE_OPTIONS: { value: string; label: string }[] = [
  { value: 'Skip', label: 'Skip' },
  { value: 'Severity', label: 'Severity' },
  { value: 'Service', label: 'Service' },
  { value: 'ResolvedAt', label: 'Resolved at' },
  { value: 'IncidentId', label: 'Incident ID' },
  { value: 'Status', label: 'Status' },
];

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

function getIconEmoji(icon: NotionDatabaseSummary['icon']): string | null {
  if (!icon) return null;
  if (typeof icon === 'string') return icon;
  if (typeof icon === 'object' && icon.emoji) return icon.emoji;
  return null;
}

// -----------------------------------------------------------------------------
// DatabasePicker: debounced async searchable combobox
// -----------------------------------------------------------------------------

interface DatabasePickerProps {
  value: string | null;
  valueTitle: string | null;
  onSelect: (db: NotionDatabaseSummary | null) => void;
  placeholder: string;
  disabled?: boolean;
}

function DatabasePicker({
  value,
  valueTitle,
  onSelect,
  placeholder,
  disabled,
}: DatabasePickerProps) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState('');
  const [debouncedQuery, setDebouncedQuery] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [results, setResults] = React.useState<NotionDatabaseSummary[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  // Debounce the query (250ms)
  React.useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 250);
    return () => clearTimeout(t);
  }, [query]);

  // Fetch when opened or when debounced query changes
  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const controller = new AbortController();
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const params = debouncedQuery
          ? `?query=${encodeURIComponent(debouncedQuery)}`
          : '';
        const res = await fetch(`/api/notion/databases${params}`, {
          credentials: 'include',
          signal: controller.signal,
        });
        const text = await res.text();
        let data: { databases?: NotionDatabaseSummary[]; error?: string } = {};
        try {
          data = text ? JSON.parse(text) : {};
        } catch {
          data = {};
        }
        if (cancelled) return;
        if (!res.ok) {
          setError(data.error || `Failed to load databases (${res.status})`);
          setResults([]);
        } else {
          setResults(Array.isArray(data.databases) ? data.databases : []);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : 'Failed to load databases');
          setResults([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [open, debouncedQuery]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={disabled}
          className="w-full justify-between font-normal"
        >
          <span className="truncate text-left">
            {value ? valueTitle || value : placeholder}
          </span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        className="w-[var(--radix-popover-trigger-width)] p-0"
        align="start"
      >
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <Search className="h-4 w-4 shrink-0 opacity-50" />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search Notion databases..."
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
        </div>
        <div className="max-h-64 overflow-y-auto py-1">
          {loading && (
            <div className="flex items-center gap-2 px-3 py-3 text-sm text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Searching...
            </div>
          )}
          {!loading && error && (
            <div className="px-3 py-3 text-sm text-destructive">{error}</div>
          )}
          {!loading && !error && results.length === 0 && (
            <div className="px-3 py-3 text-sm text-muted-foreground">
              No databases found.
            </div>
          )}
          {!loading &&
            !error &&
            results.map((db) => {
              const emoji = getIconEmoji(db.icon);
              const isSelected = db.id === value;
              return (
                <button
                  type="button"
                  key={db.id}
                  onClick={() => {
                    onSelect(db);
                    setOpen(false);
                  }}
                  className={cn(
                    'flex w-full items-start gap-2 px-3 py-2 text-left text-sm hover:bg-accent',
                    isSelected && 'bg-accent',
                  )}
                >
                  <span className="mt-0.5 w-4 shrink-0 text-center">
                    {emoji || <span className="text-muted-foreground">•</span>}
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className="block truncate font-medium">
                      {db.title || 'Untitled'}
                    </span>
                    {db.description && (
                      <span className="block truncate text-xs text-muted-foreground">
                        {db.description}
                      </span>
                    )}
                  </span>
                  {isSelected && (
                    <Check className="mt-1 h-4 w-4 shrink-0 opacity-70" />
                  )}
                </button>
              );
            })}
        </div>
      </PopoverContent>
    </Popover>
  );
}

// -----------------------------------------------------------------------------
// Main Dialog
// -----------------------------------------------------------------------------

export default function ExportToNotionDialog({
  open,
  onOpenChange,
  incidentId,
  onExported,
}: ExportToNotionDialogProps) {
  const [selectedDatabaseId, setSelectedDatabaseId] = React.useState<
    string | null
  >(null);
  const [selectedDatabaseTitle, setSelectedDatabaseTitle] = React.useState<
    string | null
  >(null);
  const [databaseDetail, setDatabaseDetail] =
    React.useState<NotionDatabaseDetail | null>(null);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [detailError, setDetailError] = React.useState<string | null>(null);

  const [propertyMapping, setPropertyMapping] = React.useState<
    Record<string, string>
  >({});

  const [useActionItems, setUseActionItems] = React.useState(false);
  const [actionItemsDatabaseId, setActionItemsDatabaseId] = React.useState<
    string | null
  >(null);
  const [actionItemsDatabaseTitle, setActionItemsDatabaseTitle] =
    React.useState<string | null>(null);

  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [reauthRequired, setReauthRequired] = React.useState(false);
  const [result, setResult] = React.useState<ExportResult | null>(null);

  // Reset state whenever dialog is opened/closed
  React.useEffect(() => {
    if (!open) {
      // Small delay so close animation doesn't flash
      const t = setTimeout(() => {
        setSelectedDatabaseId(null);
        setSelectedDatabaseTitle(null);
        setDatabaseDetail(null);
        setDetailLoading(false);
        setDetailError(null);
        setPropertyMapping({});
        setUseActionItems(false);
        setActionItemsDatabaseId(null);
        setActionItemsDatabaseTitle(null);
        setSubmitting(false);
        setSubmitError(null);
        setReauthRequired(false);
        setResult(null);
      }, 150);
      return () => clearTimeout(t);
    }
  }, [open]);

  // Fetch detail for primary DB
  React.useEffect(() => {
    if (!selectedDatabaseId) {
      setDatabaseDetail(null);
      setDetailError(null);
      setPropertyMapping({});
      return;
    }
    let cancelled = false;
    const run = async () => {
      setDetailLoading(true);
      setDetailError(null);
      try {
        const res = await fetch(
          `/api/notion/databases/${encodeURIComponent(selectedDatabaseId)}`,
          { credentials: 'include' },
        );
        const text = await res.text();
        let data: Partial<NotionDatabaseDetail> & { error?: string } = {};
        try {
          data = text ? JSON.parse(text) : {};
        } catch {
          data = {};
        }
        if (cancelled) return;
        if (!res.ok) {
          setDetailError(
            data.error || `Failed to load database (${res.status})`,
          );
          setDatabaseDetail(null);
        } else {
          setDatabaseDetail({
            id: data.id || selectedDatabaseId,
            title: data.title || selectedDatabaseTitle || '',
            titleProperty: data.titleProperty ?? null,
            properties: data.properties || {},
          });
          setPropertyMapping({});
        }
      } catch (e) {
        if (!cancelled) {
          setDetailError(
            e instanceof Error ? e.message : 'Failed to load database',
          );
          setDatabaseDetail(null);
        }
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [selectedDatabaseId, selectedDatabaseTitle]);

  const mappableProperties = React.useMemo(() => {
    if (!databaseDetail) return [];
    const titleProp = databaseDetail.titleProperty;
    return Object.entries(databaseDetail.properties)
      .filter(([name]) => name !== titleProp)
      .map(([name, meta]) => ({ name, type: meta.type }));
  }, [databaseDetail]);

  const handleSubmit = async () => {
    if (!selectedDatabaseId) return;
    setSubmitting(true);
    setSubmitError(null);
    setReauthRequired(false);

    // Filter out "Skip" mappings
    const mapping: Record<string, string> = {};
    for (const [name, source] of Object.entries(propertyMapping)) {
      if (source && source !== 'Skip') {
        mapping[name] = source;
      }
    }

    try {
      const res = await postmortemService.exportToNotion(incidentId, {
        databaseId: selectedDatabaseId,
        titleProperty: databaseDetail?.titleProperty || undefined,
        propertyMapping: Object.keys(mapping).length ? mapping : undefined,
        actionItemsDatabaseId:
          useActionItems && actionItemsDatabaseId
            ? actionItemsDatabaseId
            : undefined,
      });

      if (res.success && res.pageUrl && res.pageId) {
        const successResult: ExportResult = {
          pageUrl: res.pageUrl,
          pageId: res.pageId,
          actionItemCount: res.actionItemCount,
        };
        onExported({ pageUrl: res.pageUrl, pageId: res.pageId });
        setResult(successResult);
      } else if (res.code === 'reauth_required') {
        setReauthRequired(true);
      } else {
        setSubmitError(res.error || 'Export failed');
      }
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : 'Export failed');
    } finally {
      setSubmitting(false);
    }
  };

  const formDisabled = submitting;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Export Postmortem to Notion</DialogTitle>
          <DialogDescription>
            Save this postmortem as a new page in a Notion database.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          // -----------------------------------------------------------------
          // Success state
          // -----------------------------------------------------------------
          <div className="flex flex-col items-center gap-4 py-4 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-green-500/10">
              <CheckCircle2 className="h-6 w-6 text-green-500" />
            </div>
            <div>
              <p className="text-base font-medium">
                Postmortem exported to Notion
              </p>
              {typeof result.actionItemCount === 'number' &&
                result.actionItemCount > 0 && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Created {result.actionItemCount} action item
                    {result.actionItemCount === 1 ? '' : 's'}.
                  </p>
                )}
            </div>
            <div className="flex w-full flex-col gap-2 sm:flex-row sm:justify-center">
              <a
                href={result.pageUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center justify-center gap-1.5 rounded-md border border-input bg-background px-4 py-2 text-sm font-medium shadow-sm hover:bg-accent"
              >
                <ExternalLink className="h-4 w-4" />
                Open in Notion
              </a>
              <Button
                variant="secondary"
                onClick={() => onOpenChange(false)}
              >
                Done
              </Button>
            </div>
          </div>
        ) : (
          // -----------------------------------------------------------------
          // Form state
          // -----------------------------------------------------------------
          <div className="space-y-5">
            {reauthRequired && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertTitle>Notion credentials expired</AlertTitle>
                <AlertDescription className="flex flex-col gap-2">
                  <span>
                    Your Notion connection needs to be refreshed before you can
                    export.
                  </span>
                  <a
                    href="/notion/connect"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex w-fit items-center gap-1.5 rounded-md border border-input bg-background px-3 py-1.5 text-xs font-medium text-foreground shadow-sm hover:bg-accent"
                  >
                    <ExternalLink className="h-3 w-3" />
                    Reconnect Notion
                  </a>
                </AlertDescription>
              </Alert>
            )}

            {submitError && !reauthRequired && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertTitle>Export failed</AlertTitle>
                <AlertDescription>{submitError}</AlertDescription>
              </Alert>
            )}

            {/* 1. Database picker */}
            <div className="space-y-1.5">
              <Label>Target database</Label>
              <DatabasePicker
                value={selectedDatabaseId}
                valueTitle={selectedDatabaseTitle}
                placeholder="Select a Notion database..."
                disabled={formDisabled}
                onSelect={(db) => {
                  setSelectedDatabaseId(db?.id || null);
                  setSelectedDatabaseTitle(db?.title || null);
                }}
              />
            </div>

            {/* 2. Auto-detected title property */}
            {selectedDatabaseId && (
              <div className="space-y-1.5">
                {detailLoading ? (
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Inspecting database schema...
                  </div>
                ) : detailError ? (
                  <Alert variant="destructive">
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>{detailError}</AlertDescription>
                  </Alert>
                ) : databaseDetail?.titleProperty ? (
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="text-muted-foreground">
                      Will save title to
                    </span>
                    <Badge variant="secondary">
                      {databaseDetail.titleProperty}
                    </Badge>
                  </div>
                ) : (
                  <Alert variant="destructive">
                    <AlertCircle className="h-4 w-4" />
                    <AlertTitle>No title property found</AlertTitle>
                    <AlertDescription>
                      This database has no title property. Choose a different
                      database.
                    </AlertDescription>
                  </Alert>
                )}
              </div>
            )}

            {/* 3. Optional property mapping */}
            {databaseDetail &&
              databaseDetail.titleProperty &&
              mappableProperties.length > 0 && (
                <Accordion type="single" collapsible className="-mx-1">
                  <AccordionItem value="mapping" className="border-b-0">
                    <AccordionTrigger className="rounded-md px-1 py-2 text-sm hover:no-underline">
                      Optional: map incident fields to Notion properties
                    </AccordionTrigger>
                    <AccordionContent className="px-1">
                      <div className="space-y-2">
                        {mappableProperties.map(({ name, type }) => (
                          <div
                            key={name}
                            className="flex items-center gap-2 text-sm"
                          >
                            <div className="flex min-w-0 flex-1 items-center gap-2">
                              <span className="truncate">{name}</span>
                              <Badge
                                variant="outline"
                                className="shrink-0 text-xs"
                              >
                                {type}
                              </Badge>
                            </div>
                            <div className="w-40 shrink-0">
                              <Select
                                value={propertyMapping[name] || 'Skip'}
                                onValueChange={(v) =>
                                  setPropertyMapping((prev) => ({
                                    ...prev,
                                    [name]: v,
                                  }))
                                }
                                disabled={formDisabled}
                              >
                                <SelectTrigger className="h-8 text-xs">
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  {SOURCE_OPTIONS.map((opt) => (
                                    <SelectItem
                                      key={opt.value}
                                      value={opt.value}
                                    >
                                      {opt.label}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            </div>
                          </div>
                        ))}
                      </div>
                    </AccordionContent>
                  </AccordionItem>
                </Accordion>
              )}

            {/* 4. Optional action items DB */}
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="use-action-items"
                  checked={useActionItems}
                  onCheckedChange={(checked) => {
                    setUseActionItems(!!checked);
                    if (!checked) {
                      setActionItemsDatabaseId(null);
                      setActionItemsDatabaseTitle(null);
                    }
                  }}
                  disabled={formDisabled}
                />
                <Label
                  htmlFor="use-action-items"
                  className="text-sm font-normal"
                >
                  Also create rows in a tasks database
                </Label>
              </div>
              {useActionItems && (
                <DatabasePicker
                  value={actionItemsDatabaseId}
                  valueTitle={actionItemsDatabaseTitle}
                  placeholder="Select a tasks database..."
                  disabled={formDisabled}
                  onSelect={(db) => {
                    setActionItemsDatabaseId(db?.id || null);
                    setActionItemsDatabaseTitle(db?.title || null);
                  }}
                />
              )}
            </div>

            {/* 5. Submit */}
            <Button
              className="w-full"
              disabled={
                !selectedDatabaseId ||
                !databaseDetail?.titleProperty ||
                submitting
              }
              onClick={handleSubmit}
            >
              {submitting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Exporting...
                </>
              ) : (
                'Export to Notion'
              )}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}


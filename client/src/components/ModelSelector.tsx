"use client";

import React, { useState, useEffect, useMemo } from 'react';
import { motion } from 'framer-motion';
import { Button } from "@/components/ui/button";
import { ChevronDown, Check, Plus, CornerDownLeft } from 'lucide-react';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
} from "@/components/ui/command";
import { ProviderIcon } from "@/components/icons/provider-icons";

interface ModelOption {
  id: string;
  name: string;
  displayName: string;
  provider: string;
  tier: 'free' | 'pro' | 'premium';
  contextLength: string;
  hasReasoning: boolean;
  isSlow?: boolean;
  pricing?: string;
  featured?: boolean;
}

interface ModelSelectorProps {
  selectedModel: string;
  onModelChange: (modelId: string) => void;
  className?: string;
  disabled?: boolean;
}

// Minimal static fallback used before the catalog loads, or if the fetch fails.
// The live catalog (curated flagships ∪ every available provider's models,
// honoring ENABLED_MODELS) replaces this on mount.
const FALLBACK_MODELS: ModelOption[] = [
  { id: 'anthropic/claude-opus-4-8', name: 'claude-opus-4-8', displayName: 'Claude Opus 4.8', provider: 'anthropic', tier: 'premium', contextLength: '1M', hasReasoning: true },
  { id: 'anthropic/claude-sonnet-5', name: 'claude-sonnet-5', displayName: 'Claude Sonnet 5', provider: 'anthropic', tier: 'pro', contextLength: '1M', hasReasoning: true },
];

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  google: 'Google',
  vertex: 'Google',
  ollama: 'Ollama',
  bedrock: 'Bedrock',
  openrouter: 'OpenRouter',
};

const providerLabel = (p: string) => PROVIDER_LABELS[p] ?? (p ? p[0].toUpperCase() + p.slice(1) : 'Other');

// Turn a raw model id the user typed into a usable option (passthrough).
function customOption(id: string): ModelOption {
  const [prefix, rest] = id.includes('/') ? [id.split('/')[0], id.slice(id.indexOf('/') + 1)] : ['', id];
  return {
    id,
    name: rest,
    displayName: rest || id,
    provider: prefix || 'other',
    tier: 'pro',
    contextLength: '',
    hasReasoning: false,
  };
}

export default function ModelSelector({
  selectedModel,
  onModelChange,
  className = "",
  disabled = false,
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [models, setModels] = useState<ModelOption[]>(FALLBACK_MODELS);

  // Load the live catalog. On failure, keep the static fallback.
  useEffect(() => {
    let cancelled = false;
    fetch('/api/llm-models')
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data) => {
        if (cancelled || !Array.isArray(data?.models) || data.models.length === 0) return;
        setModels(data.models as ModelOption[]);
      })
      .catch(() => {
        /* keep FALLBACK_MODELS */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Reconcile the persisted / current selection against the loaded catalog.
  // A custom (uncatalogued) model the user previously chose is kept as-is.
  useEffect(() => {
    if (models.length === 0) return;
    const isKnown = (id: string) => models.some((m) => m.id === id);
    const savedModel = localStorage.getItem('selectedModel');
    if (savedModel && savedModel !== selectedModel && (isKnown(savedModel) || savedModel.includes('/'))) {
      onModelChange(savedModel);
    } else if (!selectedModel) {
      onModelChange(models[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models]);

  // Default view shows only featured models (curated flagships + direct-provider
  // models like Ollama pulls). As soon as the user types, search spans the full
  // catalog — cmdk filters on each item's `value`, so we just widen the source.
  const hasQuery = query.trim().length > 0;
  const featuredModels = useMemo(() => models.filter((m) => m.featured !== false), [models]);
  const hiddenCount = models.length - featuredModels.length;

  // Group the visible set by provider, preserving order within each group.
  const grouped = useMemo(() => {
    const source = hasQuery ? models : featuredModels;
    const order: string[] = [];
    const byProvider = new Map<string, ModelOption[]>();
    for (const model of source) {
      const key = providerLabel(model.provider);
      if (!byProvider.has(key)) {
        byProvider.set(key, []);
        order.push(key);
      }
      byProvider.get(key)!.push(model);
    }
    return order.map((label) => ({ label, provider: byProvider.get(label)![0].provider, items: byProvider.get(label)! }));
  }, [models, featuredModels, hasQuery]);

  const handleSelect = (modelId: string) => {
    onModelChange(modelId);
    localStorage.setItem('selectedModel', modelId);
    setIsOpen(false);
    setQuery("");
  };

  const selected =
    models.find((m) => m.id === selectedModel) ||
    (selectedModel ? customOption(selectedModel) : models[0]);

  // Offer a "use custom model" row when the query looks like a provider/model id
  // that isn't already in the list — this is the passthrough escape hatch.
  const trimmed = query.trim();
  const showCustom =
    trimmed.includes('/') && !models.some((m) => m.id.toLowerCase() === trimmed.toLowerCase());

  return (
    <Popover open={isOpen} onOpenChange={(o) => { setIsOpen(o); if (!o) setQuery(""); }}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          className={`h-8 px-2.5 justify-between min-w-[150px] max-w-[210px] text-sm font-medium text-foreground hover:bg-accent transition-colors ${className}`}
          disabled={disabled}
        >
          <div className="flex items-center min-w-0 flex-1 gap-2">
            {selected && <ProviderIcon provider={selected.provider} size={16} className="flex-shrink-0" />}
            <span className="truncate flex-1 text-left">{selected?.displayName ?? 'Select model'}</span>
          </div>
          <motion.span
            animate={{ rotate: isOpen ? 180 : 0 }}
            transition={{ type: 'spring', stiffness: 400, damping: 25 }}
            className="ml-1 flex-shrink-0"
          >
            <ChevronDown className="h-3.5 w-3.5" />
          </motion.span>
        </Button>
      </PopoverTrigger>

      <PopoverContent align="end" sideOffset={6} className="w-[320px] p-0 overflow-hidden">
        <motion.div
          initial={{ opacity: 0, y: -6, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{ type: 'spring', stiffness: 500, damping: 32 }}
        >
          <Command>
            <CommandInput
              placeholder="Search models, or type provider/model…"
              value={query}
              onValueChange={setQuery}
            />
            <CommandList>
              <CommandEmpty>
                {showCustom ? null : 'No models found.'}
              </CommandEmpty>

              {showCustom && (
                <CommandGroup heading="Custom">
                  <CommandItem
                    value={`__custom__${trimmed}`}
                    onSelect={() => handleSelect(trimmed)}
                  >
                    <Plus className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">
                      Use <span className="font-mono font-medium">{trimmed}</span>
                    </span>
                    <CornerDownLeft className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
                  </CommandItem>
                </CommandGroup>
              )}

              {grouped.map((group) => (
                <CommandGroup key={group.label} heading={group.label}>
                  {group.items.map((model) => {
                    const isSelected = model.id === selectedModel;
                    return (
                      <CommandItem
                        key={model.id}
                        // include id + provider so search matches "opus", "anthropic", "claude/…"
                        value={`${model.displayName} ${model.id} ${group.label}`}
                        onSelect={() => handleSelect(model.id)}
                        className="group"
                      >
                        <ProviderIcon
                          provider={model.provider}
                          size={17}
                          className="flex-shrink-0 opacity-80 transition-opacity group-data-[selected=true]:opacity-100"
                        />
                        <span className="min-w-0 flex-1 truncate font-medium">{model.displayName}</span>
                        {model.contextLength && (
                          <span className="flex-shrink-0 font-mono text-[11px] text-muted-foreground">
                            {model.contextLength}
                          </span>
                        )}
                        <span className="flex h-4 w-4 flex-shrink-0 items-center justify-center">
                          {isSelected && <Check className="h-4 w-4 text-primary" />}
                        </span>
                      </CommandItem>
                    );
                  })}
                </CommandGroup>
              ))}

              {!hasQuery && hiddenCount > 0 && (
                <div className="border-t px-3 py-2 text-center text-[11px] text-muted-foreground">
                  Search to browse {hiddenCount} more models
                </div>
              )}
            </CommandList>
          </Command>
        </motion.div>
      </PopoverContent>
    </Popover>
  );
}

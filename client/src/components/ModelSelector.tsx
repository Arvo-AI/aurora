"use client";

import React, { useState, useEffect, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Button } from "@/components/ui/button";
import { ChevronDown, Check } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
}

interface ModelSelectorProps {
  selectedModel: string;
  onModelChange: (modelId: string) => void;
  className?: string;
  disabled?: boolean;
}

// Static fallback used before the catalog loads, or if the fetch fails. Kept in
// sync with the backend model_catalog so the selector is never empty. The live
// catalog (which honors the ENABLED_MODELS allowlist) replaces this on mount.
const FALLBACK_MODELS: ModelOption[] = [
  { id: 'anthropic/claude-opus-4-8', name: 'claude-opus-4-8', displayName: 'Claude Opus 4.8', provider: 'anthropic', tier: 'premium', contextLength: '1M', hasReasoning: true, isSlow: true, pricing: 'High Cost ($5/$25 per 1M)' },
  { id: 'anthropic/claude-sonnet-5', name: 'claude-sonnet-5', displayName: 'Claude Sonnet 5', provider: 'anthropic', tier: 'pro', contextLength: '1M', hasReasoning: true, pricing: 'Medium Cost ($3/$15 per 1M)' },
  { id: 'anthropic/claude-fable-5', name: 'claude-fable-5', displayName: 'Claude Fable 5', provider: 'anthropic', tier: 'premium', contextLength: '1M', hasReasoning: true, isSlow: true, pricing: 'Premium Cost ($10/$50 per 1M)' },
  { id: 'anthropic/claude-haiku-4.5', name: 'claude-haiku-4.5', displayName: 'Claude Haiku 4.5', provider: 'anthropic', tier: 'free', contextLength: '200K', hasReasoning: true, pricing: 'Low Cost ($1/$5 per 1M)' },
];

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  google: 'Google',
  vertex: 'Google',
};

export default function ModelSelector({
  selectedModel,
  onModelChange,
  className = "",
  disabled = false,
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [models, setModels] = useState<ModelOption[]>(FALLBACK_MODELS);

  // Load the live catalog (honors the backend ENABLED_MODELS allowlist). On
  // failure we keep the static fallback so the selector still works offline.
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
  useEffect(() => {
    if (models.length === 0) return;
    const isValid = (id: string) => models.some((m) => m.id === id);

    const savedModel = localStorage.getItem('selectedModel');
    if (savedModel && isValid(savedModel)) {
      if (savedModel !== selectedModel) onModelChange(savedModel);
      return;
    }
    if (savedModel && !isValid(savedModel)) {
      localStorage.removeItem('selectedModel');
    }
    // If the current selection isn't offered (e.g. removed by the allowlist),
    // fall back to the first (newest/most capable) catalogued model.
    if (!isValid(selectedModel)) {
      onModelChange(models[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models]);

  // Group models by provider, preserving catalog order within each group.
  const grouped = useMemo(() => {
    const order: string[] = [];
    const byProvider = new Map<string, ModelOption[]>();
    for (const model of models) {
      const key = PROVIDER_LABELS[model.provider] ?? model.provider;
      if (!byProvider.has(key)) {
        byProvider.set(key, []);
        order.push(key);
      }
      byProvider.get(key)!.push(model);
    }
    return order.map((label) => ({ label, items: byProvider.get(label)! }));
  }, [models]);

  const handleModelSelect = (modelId: string) => {
    onModelChange(modelId);
    localStorage.setItem('selectedModel', modelId);
    setIsOpen(false);
  };

  const selectedModelData = models.find((m) => m.id === selectedModel) || models[0];

  return (
    <TooltipProvider delayDuration={150}>
      <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            className={`h-8 px-2.5 justify-between min-w-[150px] max-w-[210px] text-sm font-medium text-foreground hover:bg-accent transition-colors ${className}`}
            disabled={disabled}
          >
            <div className="flex items-center min-w-0 flex-1 gap-2">
              {selectedModelData && (
                <ProviderIcon provider={selectedModelData.provider} size={16} className="flex-shrink-0" />
              )}
              <span className="truncate flex-1 text-left">
                {selectedModelData?.displayName ?? 'Select model'}
              </span>
            </div>
            <motion.span
              animate={{ rotate: isOpen ? 180 : 0 }}
              transition={{ type: 'spring', stiffness: 400, damping: 25 }}
              className="ml-1 flex-shrink-0"
            >
              <ChevronDown className="h-3 w-3" />
            </motion.span>
          </Button>
        </DropdownMenuTrigger>

        <DropdownMenuContent
          asChild
          align="end"
          sideOffset={6}
          className="w-[300px] max-h-[380px] overflow-y-auto p-1.5"
        >
          <motion.div
            initial={{ opacity: 0, y: -6, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ type: 'spring', stiffness: 500, damping: 32 }}
          >
                {grouped.map((group, groupIdx) => (
                  <div key={group.label} className={groupIdx > 0 ? 'mt-1.5' : ''}>
                    <div className="flex items-center gap-1.5 px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                      <ProviderIcon provider={group.items[0].provider} size={13} />
                      {group.label}
                    </div>

                    {group.items.map((model, idx) => {
                      const isSelected = model.id === selectedModel;
                      return (
                        <Tooltip key={model.id}>
                          <TooltipTrigger asChild>
                            <motion.button
                              type="button"
                              onClick={() => handleModelSelect(model.id)}
                              initial={{ opacity: 0, x: -8 }}
                              animate={{ opacity: 1, x: 0 }}
                              transition={{ delay: 0.02 * idx, duration: 0.18 }}
                              className={`group relative flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left outline-none transition-colors focus-visible:bg-accent hover:bg-accent ${
                                isSelected ? 'bg-accent/60' : ''
                              }`}
                            >
                              <ProviderIcon
                                provider={model.provider}
                                size={17}
                                className="flex-shrink-0 opacity-80 transition-opacity group-hover:opacity-100"
                              />

                              <span className="min-w-0 flex-1 truncate text-sm font-medium">
                                {model.displayName}
                              </span>

                              <span className="flex-shrink-0 font-mono text-[11px] text-muted-foreground">
                                {model.contextLength}
                              </span>

                              <span className="flex h-4 w-4 flex-shrink-0 items-center justify-center">
                                <AnimatePresence>
                                  {isSelected && (
                                    <motion.span
                                      key="check"
                                      initial={{ scale: 0, opacity: 0 }}
                                      animate={{ scale: 1, opacity: 1 }}
                                      exit={{ scale: 0, opacity: 0 }}
                                      transition={{ type: 'spring', stiffness: 500, damping: 20 }}
                                    >
                                      <Check className="h-4 w-4 text-primary" />
                                    </motion.span>
                                  )}
                                </AnimatePresence>
                              </span>
                            </motion.button>
                          </TooltipTrigger>
                          {model.pricing && (
                            <TooltipContent side="left" sideOffset={8} className="text-xs">
                              <p className="font-medium">{model.pricing}</p>
                            </TooltipContent>
                          )}
                        </Tooltip>
                      );
                    })}
                  </div>
                ))}
              </motion.div>
            </DropdownMenuContent>
      </DropdownMenu>
    </TooltipProvider>
  );
}

"use client";

import React, { useState, useEffect, useMemo } from 'react';
import { Button } from "@/components/ui/button";
import { Brain, ChevronDown } from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface ModelOption {
  id: string;
  name: string;
  displayName: string;
  provider: string;
  tier: 'free' | 'pro' | 'premium';
  contextLength: string;
  hasReasoning: boolean;
  isSlow?: boolean;
}

interface ModelSelectorProps {
  selectedModel: string;
  onModelChange: (modelId: string) => void;
  className?: string;
  disabled?: boolean;
}

// Pricing information mapping (input/output per 1M tokens)
const modelPricing: Record<string, string> = {
  'openai/gpt-5.5': 'Premium Cost ($5/$30 per 1M)',
  'anthropic/claude-sonnet-4.6': 'Medium Cost ($3/$15 per 1M)',
  'anthropic/claude-opus-4.7': 'High Cost ($5/$25 per 1M)',
  'google/gemini-3.5-flash': 'Low Cost ($0.50/$3 per 1M)',
  'google/gemini-3.1-pro-preview': 'Medium Cost ($2/$12 per 1M)',
  'google/gemini-2.5-pro': 'Medium Cost ($1.25/$10 per 1M)',
  'google/gemini-2.5-flash': 'Low Cost ($0.30/$2.50 per 1M)',
  'vertex/gemini-3.6-flash': 'Low Cost ($0.75/$3.75 per 1M)',
  'vertex/gemini-3.5-flash-lite': 'Lowest Cost ($0.30/$2.50 per 1M)',
};

const modelOptions: ModelOption[] = [
  {
    id: 'openai/gpt-5.5',
    name: 'gpt-5.5',
    displayName: 'GPT-5.5',
    provider: 'OpenAI',
    tier: 'premium',
    contextLength: '1M',
    hasReasoning: true
  },
  {
    id: 'anthropic/claude-sonnet-4.6',
    name: 'claude-sonnet-4.6',
    displayName: 'Claude Sonnet 4.6',
    provider: 'Anthropic',
    tier: 'pro',
    contextLength: '1M',
    hasReasoning: true
  },
  {
    id: 'anthropic/claude-opus-4.7',
    name: 'claude-opus-4.7',
    displayName: 'Claude Opus 4.7',
    provider: 'Anthropic',
    tier: 'premium',
    contextLength: '1M',
    hasReasoning: true,
    isSlow: true
  },
  {
    id: 'google/gemini-3.5-flash',
    name: 'gemini-3.5-flash',
    displayName: 'Gemini 3.5 Flash',
    provider: 'Google',
    tier: 'free',
    contextLength: '1M',
    hasReasoning: true
  },
  {
    id: 'google/gemini-3.1-pro-preview',
    name: 'gemini-3.1-pro-preview',
    displayName: 'Gemini 3.1 Pro',
    provider: 'Google',
    tier: 'pro',
    contextLength: '1M',
    hasReasoning: true
  },
  {
    id: 'google/gemini-2.5-pro',
    name: 'gemini-2.5-pro',
    displayName: 'Gemini 2.5 Pro',
    provider: 'Google',
    tier: 'pro',
    contextLength: '1M',
    hasReasoning: true
  },
  {
    id: 'google/gemini-2.5-flash',
    name: 'gemini-2.5-flash',
    displayName: 'Gemini 2.5 Flash',
    provider: 'Google',
    tier: 'free',
    contextLength: '1M',
    hasReasoning: true
  },
  // Vertex-only — 3.6 Flash and 3.5 Flash-Lite were tested on Vertex, not Google AI.
  {
    id: 'vertex/gemini-3.6-flash',
    name: 'gemini-3.6-flash',
    displayName: 'Gemini 3.6 Flash',
    provider: 'Vertex',
    tier: 'free',
    contextLength: '1M',
    hasReasoning: true
  },
  {
    id: 'vertex/gemini-3.5-flash-lite',
    name: 'gemini-3.5-flash-lite',
    displayName: 'Gemini 3.5 Flash-Lite',
    provider: 'Vertex',
    tier: 'free',
    contextLength: '1M',
    hasReasoning: false
  },
];

interface PickerConfig {
  prefixes: string[] | null;
}

function modelsForPrefixes(prefixes: string[] | null): ModelOption[] {
  // OpenRouter / no keys: full catalog minus Vertex-only ids (those are not Google AI / OpenRouter picks).
  if (!prefixes) {
    return modelOptions.filter((m) => m.id.split('/')[0] !== 'vertex');
  }

  const out: ModelOption[] = [];
  for (const model of modelOptions) {
    const prefix = model.id.split('/')[0];
    if (prefix === 'openai' && prefixes.includes('openai')) {
      out.push(model);
    } else if (prefix === 'anthropic' && (prefixes.includes('anthropic') || prefixes.includes('bedrock'))) {
      out.push(model);
    } else if (prefix === 'google') {
      if (prefixes.includes('google')) out.push(model);
      if (prefixes.includes('vertex')) {
        out.push({ ...model, id: `vertex/${model.name}` });
      }
    } else if (prefix === 'vertex' && prefixes.includes('vertex')) {
      out.push(model);
    }
  }
  return out;
}

export default function ModelSelector({ 
  selectedModel, 
  onModelChange, 
  className = "", 
  disabled = false 
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const { data: picker, error: pickerError } = useQuery<PickerConfig>(
    '/api/llm-config/picker',
    jsonFetcher,
    { staleTime: 60_000 },
  );
  const visibleModels = useMemo(() => {
    if (pickerError) return modelsForPrefixes(null);
    if (!picker) return [];
    return modelsForPrefixes(picker.prefixes);
  }, [picker, pickerError]);

  // Load saved model from localStorage on mount, then drop anything this
  // deployment cannot actually serve (e.g. GPT under Vertex).
  useEffect(() => {
    const catalog = visibleModels;
    if (catalog.length === 0) return;

    const savedModel = localStorage.getItem('selectedModel');
    const savedName = savedModel?.split('/')[1];
    const candidate = savedModel && catalog.some((m) => m.id === savedModel)
      ? savedModel
      : savedName && catalog.find((m) => m.name === savedName)?.id
        || (catalog.some((m) => m.id === selectedModel) ? selectedModel : catalog[0].id);

    if (candidate !== selectedModel) {
      onModelChange(candidate);
    }
    if (savedModel && savedModel !== candidate) {
      localStorage.removeItem('selectedModel');
    } else if (candidate !== savedModel) {
      localStorage.setItem('selectedModel', candidate);
    }
  }, [visibleModels]);

  const handleModelSelect = (modelId: string) => {
    onModelChange(modelId);
    localStorage.setItem('selectedModel', modelId);
    setIsOpen(false);
  };

  const selectedModelData = visibleModels.find(model => model.id === selectedModel)
    || modelOptions.find(model => model.id === selectedModel)
    || visibleModels[0]
    || modelOptions[0];

  return (
    <TooltipProvider>
      <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
        <DropdownMenuTrigger asChild>
          <Button 
            variant="ghost" 
            className={`h-6 px-2 justify-between min-w-[120px] max-w-[180px] text-xs font-medium text-foreground hover:bg-muted/50 ${className}`}
            disabled={disabled}
          >
            <div className="flex items-center min-w-0 flex-1">
              {selectedModelData.hasReasoning && (
                <Brain className="w-1 h-1 mr-1" />
              )}
              <span className="truncate flex-1">{selectedModelData.displayName}</span>
            </div>
            <ChevronDown className="h-2.5 w-2.5 ml-1 flex-shrink-0" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent className="w-[280px] max-h-[300px] overflow-y-auto" align="end">
          <DropdownMenuLabel className="flex items-center gap-2 text-xs">
            <Brain className="w-4 h-4" />
            Choose Model
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          
          {visibleModels.map((model) => {
            const pricingInfo = modelPricing[model.id] || modelPricing[`google/${model.name}`];
            return (
              <Tooltip key={model.id}>
                <TooltipTrigger asChild>
                  <DropdownMenuItem
                    onClick={() => handleModelSelect(model.id)}
                    className="p-2 cursor-pointer focus:bg-muted/50 hover:bg-muted/70 transition-colors duration-200"
                  >
                    <div className="flex items-center justify-between w-full">
                      <div className="flex items-center min-w-0 flex-1">
                        {model.hasReasoning && (
                          <Brain className="w-1 h-1 flex-shrink-0 mr-1.5" />
                        )}
                        <div className="min-w-0 flex-1">
                        <span className="font-medium text-xs truncate">{model.displayName}</span>
                      </div>
                    </div>
                    <div className="flex flex-col items-end text-xs text-muted-foreground flex-shrink-0 ml-2">
                      <span className="font-mono">{model.contextLength}</span>
                    </div>
                  </div>
                  </DropdownMenuItem>
                </TooltipTrigger>
                {(pricingInfo || model.isSlow) && (
                  <TooltipContent 
                    className="bg-black text-yellow-400 border-gray-600 text-xs" 
                    side="left"
                    sideOffset={5}
                  >
                    {pricingInfo && <p className="font-medium">{pricingInfo}</p>}
                    {model.isSlow && <p className="font-medium text-orange-400">{pricingInfo ? '• ' : ''}Heavy slow reasoning</p>}
                  </TooltipContent>
                )}
              </Tooltip>
            );
          })}
          
          <DropdownMenuSeparator />
          <div className="p-1.5 text-xs text-muted-foreground text-center">
            AI Model Selection
          </div>
        </DropdownMenuContent>
      </DropdownMenu>
    </TooltipProvider>
  );
} 

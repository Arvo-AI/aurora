"use client";

import React, { useState, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { Brain, ChevronDown } from 'lucide-react';
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
  'openai/gpt-5.2': 'Premium Cost ($1.75/$14 per 1M)',
  'anthropic/claude-sonnet-4-5': 'Medium Cost ($3/$15 per 1M)',
  'anthropic/claude-opus-4-5': 'High Cost ($5/$25 per 1M)',
  'google/gemini-3-pro-preview': 'Medium Cost ($1.25/$10 per 1M)',
};

const modelOptions: ModelOption[] = [
  {
    id: 'openai/gpt-5.2',
    name: 'gpt-5.2',
    displayName: 'GPT-5.2',
    provider: 'OpenAI',
    tier: 'premium',
    contextLength: '400K',
    hasReasoning: true
  },
  {
    id: 'anthropic/claude-sonnet-4-5',
    name: 'claude-sonnet-4-5',
    displayName: 'Claude Sonnet 4.5',
    provider: 'Anthropic',
    tier: 'pro',
    contextLength: '200K',
    hasReasoning: true
  },
  {
    id: 'anthropic/claude-opus-4-5',
    name: 'claude-opus-4-5',
    displayName: 'Claude Opus 4.5',
    provider: 'Anthropic',
    tier: 'premium',
    contextLength: '200K',
    hasReasoning: true,
    isSlow: true
  },
  {
    id: 'google/gemini-3-pro-preview',
    name: 'gemini-3-pro-preview',
    displayName: 'Gemini 3 Pro',
    provider: 'Google',
    tier: 'pro',
    contextLength: '1M',
    hasReasoning: true
  },
];

export default function ModelSelector({ 
  selectedModel, 
  onModelChange, 
  className = "", 
  disabled = false 
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  
  // Load saved model from localStorage on mount
  useEffect(() => {
    const savedModel = localStorage.getItem('selectedModel');
    if (!savedModel) return;

    const isValidModel = modelOptions.some((model) => model.id === savedModel);
    if (isValidModel && savedModel !== selectedModel) {
      onModelChange(savedModel);
    } else if (!isValidModel) {
      localStorage.removeItem('selectedModel');
    }
  }, []);

  const handleModelSelect = (modelId: string) => {
    onModelChange(modelId);
    localStorage.setItem('selectedModel', modelId);
    setIsOpen(false);
  };

  const selectedModelData = modelOptions.find(model => model.id === selectedModel) || modelOptions[0];

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
          
          {modelOptions.map((model) => {
            const pricingInfo = modelPricing[model.id];
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

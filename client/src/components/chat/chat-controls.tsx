"use client";

import ModelSelector from "@/components/ModelSelector";
import ModeSelector from "@/components/ModeSelector";
import { Button } from "@/components/ui/button";
import { Radar } from "lucide-react";
import { useConnectedAccounts } from "@/hooks/useConnectedAccounts";

interface ChatControlsProps {
  selectedModel: string;
  onModelChange: (model: string) => void;
  selectedMode: string;
  onModeChange: (mode: string) => void;
  selectedProviders?: string[]; // Kept for compatibility but not used
  disabled?: boolean;
  className?: string;
  rcaActive?: boolean;
  onToggleRCA?: () => void;
}

export default function ChatControls({
  selectedModel,
  onModelChange,
  selectedMode,
  onModeChange,
  disabled = false,
  className = "",
  rcaActive = false,
  onToggleRCA,
}: ChatControlsProps) {
  // Get all connected providers from database
  const { infraProviders } = useConnectedAccounts();

  return (
    <div className={`flex items-center justify-between ${className}`}>
      {/* Left: Mode selector + Trigger RCA */}
      <div className="flex items-center gap-1">
        <ModeSelector
          selectedMode={selectedMode}
          onModeChange={onModeChange}
          disabled={disabled}
          className="h-7 text-sm"
        />
        {onToggleRCA && (
          <Button
            variant="ghost"
            onClick={onToggleRCA}
            disabled={disabled}
            aria-pressed={rcaActive}
            className={`h-6 px-2 text-xs font-medium transition-colors gap-1 ${
              rcaActive
                ? "text-orange-500 bg-orange-500/15 hover:bg-orange-500/20"
                : "text-muted-foreground hover:text-orange-500 hover:bg-orange-500/10"
            }`}
            title={rcaActive ? "RCA mode active — click Send to investigate" : "Enable RCA mode"}
          >
            <Radar className="h-3 w-3" />
            <span>RCA</span>
          </Button>
        )}
      </div>

      {/* Right: Model selector + Provider logos */}
      <div className="flex items-center space-x-2">
        <ModelSelector
          selectedModel={selectedModel}
          onModelChange={onModelChange}
          disabled={disabled}
          className="h-7 text-sm"
        />
        {infraProviders.length > 0 && (
          <div className="flex items-center space-x-1 ml-1">
            {infraProviders.map((provider, index) => (
              <div
                key={`${provider.id}-${index}`}
                className="relative w-6 h-6 flex-shrink-0"
                title={provider.name}
              >
                <img
                  src={provider.icon}
                  alt={provider.name}
                  className={`w-full h-full object-contain ${
                    provider.id === 'aws' ? 'bg-white rounded-sm p-0.5' : ''
                  }`}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

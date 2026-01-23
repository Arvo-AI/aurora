"use client";

import ModelSelector from "@/components/ModelSelector";
import ModeSelector from "@/components/ModeSelector";
import { useSelectedProviders } from "@/hooks/useSelectedProviders";

interface ChatControlsProps {
  selectedModel: string;
  onModelChange: (model: string) => void;
  selectedMode: string;
  onModeChange: (mode: string) => void;
  selectedProviders?: string[]; // Kept for compatibility but not used
  disabled?: boolean;
  className?: string;
}

export default function ChatControls({
  selectedModel,
  onModelChange,
  selectedMode,
  onModeChange,
  disabled = false,
  className = ""
}: ChatControlsProps) {
  // Get all connected providers from database
  const selectedConnectedProviders = useSelectedProviders();

  return (
    <div className={`flex items-center justify-between ${className}`}>
      {/* Left: Mode selector only */}
      <div className="flex items-center">
        <ModeSelector
          selectedMode={selectedMode}
          onModeChange={onModeChange}
          disabled={disabled}
          className="h-7 text-sm"
        />
      </div>

      {/* Right: Model selector + Provider logos */}
      <div className="flex items-center space-x-2">
        <ModelSelector
          selectedModel={selectedModel}
          onModelChange={onModelChange}
          disabled={disabled}
          className="h-7 text-sm"
        />
        {selectedConnectedProviders.length > 0 && (
          <div className="flex items-center space-x-1 ml-1">
            {selectedConnectedProviders.map((provider, index) => (
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

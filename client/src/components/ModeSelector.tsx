"use client";

import React, { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Infinity, Bot, MessageSquare, ChevronDown } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface ModeOption {
  id: string;
  displayName: string;
  icon: JSX.Element;
}

interface ModeSelectorProps {
  selectedMode: string;
  onModeChange: (modeId: string) => void;
  className?: string;
  disabled?: boolean;
}

const modeOptions: ModeOption[] = [
  {
    id: "ask",
    displayName: "Ask (Read Mode)",
    icon: <MessageSquare className="w-2 h-2 flex-shrink-0" />,
  },
  {
    id: "agent",
    displayName: "Agent",
    icon: <Infinity className="w-2 h-2 flex-shrink-0" />,
  },
  {
    id: "sandbox",
    displayName: "Testing Sandbox",
    icon: <Bot className="w-2 h-2 flex-shrink-0" />,
  },
];

export default function ModeSelector({
  selectedMode,
  onModeChange,
  className = "",
  disabled = false,
}: ModeSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);

  // Load saved mode from localStorage on mount
  useEffect(() => {
    const savedMode = localStorage.getItem("selectedMode");
    if (savedMode && savedMode !== selectedMode) {
      onModeChange(savedMode);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleModeSelect = (modeId: string) => {
    onModeChange(modeId);
    localStorage.setItem("selectedMode", modeId);
    setIsOpen(false);
  };

  const selectedModeData =
    modeOptions.find((mode) => mode.id === selectedMode) || modeOptions[0];

  return (
    <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className={`h-6 px-2 justify-between min-w-[90px] text-xs font-medium text-foreground hover:bg-muted/50 ${className}`}
          disabled={disabled}
        >
          <div className="flex items-center min-w-0 flex-1">
            {selectedModeData.icon}
            <span className="truncate flex-1 ml-1">
              {selectedModeData.displayName}
            </span>
          </div>
          <ChevronDown className="h-2.5 w-2.5 ml-1 flex-shrink-0" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-[160px]" align="start">
        <DropdownMenuLabel className="text-xs">Mode</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {modeOptions.map((mode) => (
          <DropdownMenuItem
            key={mode.id}
            onClick={() => handleModeSelect(mode.id)}
            className="p-2 cursor-pointer focus:bg-muted/50"
          >
            <div className="flex items-center">
              {mode.icon}
              <span className="ml-2 text-xs">{mode.displayName}</span>
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
} 

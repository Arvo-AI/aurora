"use client";

import React, { useCallback, useState } from "react";
import { Send, Loader2, Square, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AutoResizeTextarea } from "@/components/ui/auto-resize-textarea";
import ChatControls from "./chat-controls";
import SlashCommandMenu, { ActionItem, getFilteredActions } from "./SlashCommandMenu";

export interface ImageAttachment {
  file: File;
  preview: string;
}

interface EnhancedChatInputProps {
  input: string;
  setInput: (value: string) => void;
  onSend: () => void;
  rcaActive?: boolean;
  onToggleRCA?: () => void;
  isSending: boolean;
  selectedModel: string;
  onModelChange: (model: string) => void;
  selectedMode: string;
  onModeChange: (mode: string) => void;
  selectedProviders?: string[];
  disabled?: boolean;
  placeholder?: string;
  className?: string;
  onCancel?: () => void;
  incidentContext?: string;
  onRemoveContext?: () => void;
  images?: ImageAttachment[];
  onImagesChange?: (images: ImageAttachment[]) => void;
  actions?: ActionItem[];
  selectedAction?: ActionItem | null;
  onActionSelect?: (action: ActionItem | null) => void;
}

export default function EnhancedChatInput({
  input,
  setInput,
  onSend,
  rcaActive = false,
  onToggleRCA,
  isSending,
  selectedModel,
  onModelChange,
  selectedMode,
  onModeChange,
  selectedProviders = [],
  disabled = false,
  placeholder = "Ask anything...",
  className = "",
  onCancel,
  incidentContext,
  onRemoveContext,
  images = [],
  onImagesChange,
  actions = [],
  selectedAction = null,
  onActionSelect,
}: EnhancedChatInputProps) {
  const [isDragOver, setIsDragOver] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(0);

  // Show menu as soon as "/" is typed (word-boundary: start of input or after space)
  const slashMatch = !selectedAction && input.match(/(^|\s)(\/\S*)$/);
  const slashToken = slashMatch?.[2] ?? '';
  const isFullCommand = /^\/actions?$/i.test(slashToken);
  const isPartialCommand = !isFullCommand && '/action'.startsWith(slashToken.toLowerCase()) && slashToken.length >= 1;
  // Also detect when command is complete and user is picking an action (e.g. "/action " or "/action qu")
  const hasCompletedCommand = !selectedAction && /(^|\s)\/actions?\s/i.test(input);
  const menuVisible = isFullCommand || isPartialCommand || hasCompletedCommand;
  const menuStage: 'command' | 'action' = (isFullCommand || hasCompletedCommand) ? 'action' : 'command';
  const filteredActions = menuVisible && menuStage === 'action' ? getFilteredActions(input, actions).slice(0, 8) : [];
  const menuItemCount = menuStage === 'command' ? 1 : (filteredActions.length || 1);

  let parsedContext = null;
  try {
    parsedContext = incidentContext ? JSON.parse(incidentContext) : null;
  } catch (e) {
    console.error('Failed to parse incident context:', e);
  }

  const handleSelect = useCallback((action: ActionItem) => {
    onActionSelect?.(action);
    // Replace /action... portion with the action name inline in the textarea
    const replaced = input.replace(/(^|\s)\/actions?\s*\S*$/i, `$1/action ${action.name} `);
    setInput(replaced === input ? `/action ${action.name} ` : replaced);
    setHighlightedIndex(0);
  }, [onActionSelect, setInput, input]);

  const handleCommandSelect = useCallback(() => {
    // Replace the partial slash token with the full /action command
    const replaced = input.replace(/(^|\s)(\/\S*)$/, '$1/action ');
    setInput(replaced);
    setHighlightedIndex(0);
  }, [input, setInput]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (menuVisible && menuItemCount > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setHighlightedIndex(i => (i + 1) % menuItemCount);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setHighlightedIndex(i => (i - 1 + menuItemCount) % menuItemCount);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        if (menuStage === 'command') {
          handleCommandSelect();
        } else {
          handleSelect(filteredActions[highlightedIndex]);
        }
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setInput('');
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if ((input.trim() || selectedAction) && !isSending && !disabled) {
        onSend();
      }
    }
  }, [input, isSending, disabled, onSend, menuVisible, menuItemCount, menuStage, filteredActions, highlightedIndex, handleSelect, handleCommandSelect, setInput, selectedAction]);

  const processImageFiles = useCallback((files: File[]) => {
    const imageFiles = files.filter(f => f.type.startsWith('image/'));
    if (imageFiles.length === 0) return;

    const newImages: ImageAttachment[] = [];
    imageFiles.forEach(file => {
      const reader = new FileReader();
      reader.onload = (e) => {
        if (e.target?.result) {
          newImages.push({
            file,
            preview: e.target.result as string
          });
          if (newImages.length === imageFiles.length) {
            onImagesChange?.([...images, ...newImages]);
          }
        }
      };
      reader.readAsDataURL(file);
    });
  }, [images, onImagesChange]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = Array.from(e.clipboardData.items);
    const files = items
      .filter(item => item.type.startsWith('image/'))
      .map(item => item.getAsFile())
      .filter((file): file is File => file !== null);
    
    if (files.length > 0) {
      e.preventDefault();
      processImageFiles(files);
    }
  }, [processImageFiles]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    processImageFiles(files);
  }, [processImageFiles]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  }, []);

  const removeImage = useCallback((index: number) => {
    const newImages = images.filter((_, i) => i !== index);
    onImagesChange?.(newImages);
  }, [images, onImagesChange]);

  return (
    <div className={`w-full max-w-4xl mx-auto space-y-3 ${className}`}>
      {/* Input container with original styling */}
      <div
        className={`bg-muted rounded-3xl border border-input p-4 space-y-3 transition-colors ${
          isDragOver ? 'border-primary bg-primary/5' : ''
        }`}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
      >
        {/* Context chips */}
        {parsedContext && (
          <div className="flex flex-wrap gap-2">
            <div className="flex items-center gap-2 px-3 py-1.5 bg-orange-500/10 border border-orange-500/30 rounded-lg text-xs w-fit">
                <span className="text-orange-400 font-medium">Incident {parsedContext.title}</span>
                {onRemoveContext && (
                  <button onClick={onRemoveContext} className="text-orange-400 hover:text-orange-300">
                    <X className="h-3 w-3" />
                  </button>
                )}
            </div>
          </div>
        )}
        
        {/* Text input area */}
        <div className="relative">
          {menuVisible && (
            <SlashCommandMenu
              input={input}
              actions={actions}
              onSelect={handleSelect}
              onCommandSelect={handleCommandSelect}
              highlightedIndex={highlightedIndex}
              stage={menuStage}
            />
          )}
          {/* Styled overlay to highlight /action Name in bold blue */}
          {selectedAction && (() => {
            const pattern = new RegExp(`(\\/actions?\\s+${selectedAction.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'i');
            const parts = input.split(pattern);
            if (parts.length < 2) return null;
            return (
              <div
                aria-hidden="true"
                className="absolute inset-0 py-2 pl-2 pr-24 text-base whitespace-pre-wrap break-words pointer-events-none overflow-hidden"
              >
                {parts.map((part, i) =>
                  pattern.test(part)
                    ? <span key={i} className="text-blue-400">{part}</span>
                    : <span key={i} className="text-foreground">{part}</span>
                )}
              </div>
            );
          })()}
          <AutoResizeTextarea
            placeholder={placeholder}
            value={input}
            onChange={(e) => { setInput(e.target.value); setHighlightedIndex(0); }}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            className={`w-full bg-transparent border-0 py-2 pl-2 pr-24 focus:ring-0 focus:outline-none text-base placeholder:text-muted-foreground resize-none ${selectedAction && input.toLowerCase().includes(`/action ${selectedAction.name.toLowerCase()}`) ? 'caret-white text-transparent' : ''}`}
            maxRows={6}
          />
          
          {/* Send/Cancel button */}
          <div className="absolute right-2 bottom-2 flex items-center space-x-2">
            {isSending && onCancel ? (
              <Button
                onClick={onCancel}
                className="rounded-full h-8 w-8 p-0 flex items-center justify-center bg-transparent hover:bg-muted border-0 shadow-none"
                aria-label="Stop generating"
              >
                <Square className="h-5 w-5 text-muted-foreground" />
              </Button>
            ) : (
              <Button
                onClick={onSend}
                disabled={!(input.trim() || selectedAction) || isSending || disabled}
                className="rounded-full h-8 w-8 p-0 bg-muted hover:bg-muted/80 border-0 shadow-none text-muted-foreground hover:text-foreground"
                size="sm"
              >
                {isSending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Send className="h-4 w-4" />
                )}
              </Button>
            )}
          </div>
        </div>

        {/* Image thumbnails */}
        {images.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-2">
            {images.map((img, idx) => (
              <div key={idx} className="relative group">
                <img 
                  src={img.preview} 
                  alt={img.file.name}
                  className="h-20 w-20 object-cover rounded-lg border border-input"
                />
                <button
                  onClick={() => removeImage(idx)}
                  className="absolute -top-2 -right-2 bg-background border border-input rounded-full p-1 opacity-0 group-hover:opacity-100 transition-opacity"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Controls row */}
        <ChatControls
          selectedModel={selectedModel}
          onModelChange={onModelChange}
          selectedMode={selectedMode}
          onModeChange={onModeChange}
          selectedProviders={selectedProviders}
          rcaActive={rcaActive}
          onToggleRCA={onToggleRCA}
        />
      </div>
    </div>
  );
}

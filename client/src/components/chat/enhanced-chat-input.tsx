"use client";

import React, { useCallback, useState } from "react";
import { Send, Loader2, Square, X, ImageIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AutoResizeTextarea } from "@/components/ui/auto-resize-textarea";
import ChatControls from "./chat-controls";

export interface ImageAttachment {
  file: File;
  preview: string;
}

interface EnhancedChatInputProps {
  input: string;
  setInput: (value: string) => void;
  onSend: () => void;
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
}

export default function EnhancedChatInput({
  input,
  setInput,
  onSend,
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
  onImagesChange
}: EnhancedChatInputProps) {
  const [isDragOver, setIsDragOver] = useState(false);
  
  let parsedContext = null;
  try {
    parsedContext = incidentContext ? JSON.parse(incidentContext) : null;
  } catch (e) {
    console.error('Failed to parse incident context:', e);
  }

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (input.trim() && !isSending && !disabled) {
        onSend();
      }
    }
  }, [input, isSending, disabled, onSend]);

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
        {/* Incident context chip */}
        {parsedContext && (
          <div className="flex items-center gap-2 px-3 py-1.5 bg-orange-500/10 border border-orange-500/30 rounded-lg text-xs w-fit">
            <span className="text-orange-400 font-medium">Incident {parsedContext.title}</span>
            {onRemoveContext && (
              <button onClick={onRemoveContext} className="text-orange-400 hover:text-orange-300">
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        )}
        
        {/* Text input area */}
        <div className="relative">
          <AutoResizeTextarea
            placeholder={placeholder}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            className="w-full bg-transparent border-0 py-2 pl-2 pr-24 focus:ring-0 focus:outline-none text-base placeholder:text-muted-foreground resize-none"
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
                disabled={!input.trim() || isSending || disabled}
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
        />
      </div>
    </div>
  );
}

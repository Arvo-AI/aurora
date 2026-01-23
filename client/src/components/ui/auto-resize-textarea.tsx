import React, { forwardRef, useCallback, useRef, useImperativeHandle, useEffect } from "react";
import { cn } from "@/lib/utils";

interface AutoResizeTextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  onEnter?: () => void;
  maxRows?: number;
}

const AutoResizeTextarea = forwardRef<HTMLTextAreaElement, AutoResizeTextareaProps>(
  ({ className, onEnter, maxRows = 5, onKeyDown, ...props }, ref) => {
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const shadowRef = useRef<HTMLDivElement>(null);
    const rafRef = useRef<number | null>(null);
    
    useImperativeHandle(ref, () => textareaRef.current!);
    
    const adjustHeight = useCallback(() => {
      const textarea = textareaRef.current;
      const shadow = shadowRef.current;
      if (!textarea || !shadow) return;

      // Sync essential styles to shadow
      shadow.style.width = `${textarea.clientWidth}px`;
      shadow.style.padding = getComputedStyle(textarea).padding;
      shadow.style.fontFamily = getComputedStyle(textarea).fontFamily;
      shadow.style.fontSize = getComputedStyle(textarea).fontSize;
      shadow.style.lineHeight = getComputedStyle(textarea).lineHeight;
      shadow.style.letterSpacing = getComputedStyle(textarea).letterSpacing;
      shadow.style.boxSizing = getComputedStyle(textarea).boxSizing;

      // Set content - add zero-width space to measure properly
      shadow.textContent = textarea.value + '\u200B';

      const scrollHeight = shadow.scrollHeight;
      const lineHeight = parseInt(window.getComputedStyle(textarea).lineHeight);
      const maxHeight = lineHeight * maxRows;
      const newHeight = Math.min(scrollHeight, maxHeight);

      const newHeightStr = `${newHeight}px`;
      if (textarea.style.height !== newHeightStr) {
        textarea.style.height = newHeightStr;
      }
      
      rafRef.current = null;
    }, [maxRows]);
    
    // Debounced height adjustment to reduce frequency of updates
    const debouncedAdjustHeight = useCallback(() => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
      }
      rafRef.current = requestAnimationFrame(adjustHeight);
    }, [adjustHeight]);
    
    useEffect(() => {
      // Only adjust height when value actually changes
      if (props.value !== undefined) {
        debouncedAdjustHeight();
      }
      
      // Cleanup on unmount
      return () => {
        if (rafRef.current) {
          cancelAnimationFrame(rafRef.current);
        }
      };
    }, [props.value, debouncedAdjustHeight]);
    
    const handleInput = useCallback(() => {
      debouncedAdjustHeight();
    }, [debouncedAdjustHeight]);
    const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        onEnter?.();
      }
      onKeyDown?.(e);
    }, [onEnter, onKeyDown]);
    
    return (
      <>
        <textarea
          ref={textareaRef}
          className={cn(
            "flex min-h-[40px] w-full rounded-md border-0 bg-transparent px-2 py-2 text-base ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-0 disabled:cursor-not-allowed disabled:opacity-50 resize-none overflow-y-auto",
            className
          )}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          rows={1}
          {...props}
        />
        <div 
          ref={shadowRef} 
          aria-hidden="true"
          style={{
            position: 'absolute',
            top: '-9999px',
            left: '-9999px',
            overflow: 'hidden',
            visibility: 'hidden',
            whiteSpace: 'pre-wrap',
            wordWrap: 'break-word'
          }}
        />
      </>
    );
  }
);

AutoResizeTextarea.displayName = "AutoResizeTextarea";

export { AutoResizeTextarea }; 
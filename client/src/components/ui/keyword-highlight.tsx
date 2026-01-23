"use client";

import React from "react";
import { cn } from "@/lib/utils";

interface KeywordHighlightProps {
  text: string;
  className?: string;
}

// Simple keyword highlighting for text content
const highlightKeywords = (text: string): JSX.Element[] => {
  // For now, just return the text as-is since we'll use language detection
  return [<span key="text">{text}</span>];
};

// Function to process text and return highlighted JSX
export const processTextWithKeywords = (text: string): JSX.Element[] => {
  return highlightKeywords(text);
};

export const KeywordHighlight: React.FC<KeywordHighlightProps> = ({
  text,
  className
}) => {
  const highlightedElements = highlightKeywords(text);
  
  return (
    <span className={cn("inline", className)}>
      {highlightedElements}
    </span>
  );
};

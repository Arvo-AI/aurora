"use client";

import React, { useMemo } from "react";
import ReactMarkdown from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { CodeBlock } from "./code-block";
import { processTextWithKeywords } from "./keyword-highlight";
import { getLanguageFromCode } from "@/utils/language-detection";
import { cn } from "@/lib/utils";

interface MarkdownRendererProps {
  content: string;
  className?: string;
  severity?: "info" | "error" | "success";
}

// Custom components for ReactMarkdown
const components = {
  p: ({ children, ...props }: any) => {
    // Process paragraph text to highlight keywords inline
    const processChildren = (child: any): any => {
      if (typeof child === 'string') {
        // Use language detection to determine if this should be treated as text with keywords
        const detectedLanguage = getLanguageFromCode(child);
        if (detectedLanguage === 'text') {
          return processTextWithKeywords(child);
        }
        return child;
      }
      if (React.isValidElement(child) && (child as any).props?.children) {
        const element = child as React.ReactElement<any>;
        return React.cloneElement(element, {
          ...(element.props as any),
          children: Array.isArray(element.props.children) 
            ? element.props.children.map(processChildren)
            : processChildren(element.props.children)
        });
      }
      return child;
    };

    const processedChildren = Array.isArray(children) 
      ? children.map(processChildren)
      : processChildren(children);

    return <p {...props}>{processedChildren}</p>;
  },
  code: ({ node, inline, className, children, ...props }: any) => {
    const match = /language-(\w+)/.exec(className || "");
    const explicitLang = match ? match[1] : undefined;

    // Helper to safely extract raw string from children (avoids [object Object])
    const extractString = (ch: any): string => {
      if (typeof ch === 'string' || typeof ch === 'number') return String(ch);
      if (Array.isArray(ch)) return ch.map(extractString).join('');
      if (typeof ch === 'object' && ch && (ch as any).props?.children) {
        return extractString((ch as any).props.children);
      }
      return '';
    };

    const rawCode = extractString(children).replace(/\n$/, "");

    // Check if this should be rendered as plain text instead of styled code
    const isCloudProviderTerm = (text: string): boolean => {
      const cloudTermPatterns = [
        // AWS regions
        /^(us|eu|ap|ca|sa|af|me|us-gov)-(east|west|central|north|south|southeast|northeast)-[1-9]$/,
        // GCP regions
        /^(us|europe|asia)-(east|west|central|north|south)[1-9]?(-[a-z])?$/,
        // Azure regions
        /^(east|west|central|north|south)(us|europe|asia|australia|brazil|canada|france|germany|india|japan|korea|norway|south africa|switzerland|uae|uk)[1-9]?$/,
        // Simple cloud provider terms
        /^(google|aws|azure|gcp)$/i,
        // Project/subscription IDs (common patterns)
        /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/,
        // Resource group names
        /^[a-zA-Z][a-zA-Z0-9_-]*$/
      ];
      
      return cloudTermPatterns.some(pattern => pattern.test(text.trim()));
    };

    if (inline) {
      // If it's a cloud provider term, render as plain text
      if (isCloudProviderTerm(rawCode)) {
        return <span className="text-foreground">{rawCode}</span>;
      }
      
      return (
        <code
          className={cn(
            "rounded-md bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200 px-2 py-1 font-mono text-sm border border-gray-200 dark:border-gray-600",
            className
          )}
          {...props}
        >
          {rawCode}
        </code>
      );
    }

    const detectedLanguage = explicitLang || getLanguageFromCode(rawCode);

    // If it's plain text, render as inline code instead of block
    if (detectedLanguage === 'text') {
      return (
        <code
          className={cn(
            "rounded-md bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200 px-2 py-1 font-mono text-sm border border-gray-200 dark:border-gray-600",
            className
          )}
          {...props}
        >
          {rawCode}
        </code>
      );
    }

    return (
      <CodeBlock
        code={rawCode}
        language={detectedLanguage}
        className="my-4"
      />
    );
  },
  pre: ({ children, ...props }: any) => {
    // If the pre contains a code block, let the code component handle it
    if (React.isValidElement(children) && children.type === components.code) {
      return children;
    }
    
    // Otherwise, render as a regular pre element
    return (
      <pre
        className="overflow-x-auto rounded-lg bg-muted p-4 text-sm"
        {...props}
      >
        {children}
      </pre>
    );
  },
};

// Utility function to convert standalone language identifiers followed by plain text
// into fenced code blocks so that ReactMarkdown renders them with our CodeBlock component.
const normalizeCodeBlocks = (markdown: string): string => {
  // Skip processing if the markdown already contains inline code with backticks
  // to avoid interfering with existing inline code formatting
  if (markdown.includes('`') && !markdown.includes('```')) {
    return markdown;
  }
  
  const languages = [
    "hcl",
    "terraform",
    "json",
    "yaml",
    "yml",
    "bash",
    "shell",
    "powershell",
    "python",
    "javascript",
    "typescript",
    "jsx",
    "tsx",
    "docker",
    "sql",
    "go",
    "rust",
    "php",
    "ruby",
    "java",
    "c",
    "cpp",
    "csharp",
    "css",
    "scss",
    "xml",
    "html",
    "ini",
    "toml",
    "properties"
  ];

  const lines = markdown.split("\n");
  const normalized: string[] = [];
  let i = 0;

  const isHeading = (line: string) => /^(#{1,6}\s)/.test(line.trim());
  const isListItem = (line: string) => /^\s*[-*+]\s/.test(line) || /^\s*\d+\.\s/.test(line);
  const isCodeFence = (line: string) => /^```/.test(line.trim());
  const isCodePattern = (line: string) => {
    const trimmed = line.trim();
    if (trimmed === "") return false;
    
    // Enhanced code pattern detection
    return /[{}[\];(),=<>+\-*/%&|!]/.test(trimmed) || 
           /^(resource|data|variable|output|provider|terraform|module|locals|required_providers|required_version|def|import|function|const|let|var|FROM|SELECT|INSERT|UPDATE|DELETE|#!)/.test(trimmed) ||
           /^(ami-|subnet-|vpc-|sg-|igw-|rtb-|t[23]\.|m[0-9]+\.|c[0-9]+\.|r[0-9]+\.|i[0-9]+\.)/.test(trimmed) ||
           trimmed.includes('{') || trimmed.includes('}') ||
           trimmed.includes('[') || trimmed.includes(']') ||
           trimmed.includes('(') || trimmed.includes(')') ||
           trimmed.includes('=') || trimmed.includes(':') ||
           (trimmed.includes('"') && trimmed.includes(':')) ||
           (trimmed.includes("'") && trimmed.includes(':')) ||
           // TypeScript/JavaScript patterns
           /\b(interface|type|enum|class|function|const|let|var|import|export)\s/.test(trimmed) ||
           // Python patterns
           /\b(def|class|import|from|if|for|while|try|except|finally)\s/.test(trimmed) ||
           // Shell patterns
           /\b(echo|cd|ls|mkdir|rm|cp|mv|grep|find|cat|chmod|sudo)\s/.test(trimmed) ||
           // SQL patterns
           /\b(SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|CREATE|DROP|TABLE)\s/i.test(trimmed) ||
           // HCL/Terraform specific patterns
           /\b(variable|resource|data|output|provider|module|locals|terraform)\s+"/.test(trimmed) ||
           /\b(description|type|default|required_providers|required_version)\s*=/.test(trimmed) ||
           // Configuration patterns
           /^\s*[a-zA-Z_][a-zA-Z0-9_-]*\s*[:=]/.test(trimmed);
  };

  const stopConditions = (line: string, lang: string) => {
    const trimmed = line.trim();
    return isHeading(trimmed) ||
           languages.includes(trimmed.toLowerCase()) ||
           isCodeFence(trimmed) ||
           // Stop at markdown elements that clearly indicate end of code
           /^\s*[-*+]\s/.test(line) || // List items
           /^\s*\d+\.\s/.test(line) || // Numbered lists
           /^\s*>\s/.test(line) || // Blockquotes
           // Stop at clear text paragraphs (sentences ending with punctuation)
           (/[.!?]\s*$/.test(trimmed) && trimmed.length > 30 && !isCodePattern(line)) ||
           // Stop at horizontal rules
           /^[-*_]{3,}$/.test(trimmed);
  };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // Skip already properly fenced code blocks
    if (isCodeFence(trimmed)) {
      normalized.push(line);
      i += 1;
      // Find the closing fence
      while (i < lines.length && !isCodeFence(lines[i].trim())) {
        normalized.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) {
        normalized.push(lines[i]); // Include closing fence
        i += 1;
      }
      continue;
    }

    // Detect a line that is exactly a language id (case-insensitive)
    if (languages.includes(trimmed.toLowerCase()) && !isListItem(line)) {
      const lang = trimmed.toLowerCase();
      let codeContent: string[] = [];
      let hasCodeContent = false;
      let hasNonEmptyContent = false;
      
      // Look ahead to see if there's actual code content
      let j = i + 1;
      
      // Skip empty lines immediately after language identifier
      while (j < lines.length && lines[j].trim() === "") {
        codeContent.push(lines[j]);
        j++;
      }
      
      while (j < lines.length && !stopConditions(lines[j], lang)) {
        const nextLine = lines[j];
        const nextTrimmed = nextLine.trim();
        
        if (nextTrimmed !== "") {
          hasNonEmptyContent = true;
          if (isCodePattern(nextLine)) {
            hasCodeContent = true;
          }
        }
        
        codeContent.push(nextLine);
        j++;
      }
      
      // Remove trailing empty lines from code content
      while (codeContent.length > 0 && codeContent[codeContent.length - 1].trim() === "") {
        codeContent.pop();
      }
      
      // Only create a code block if there's actual code content and non-empty content
      if (hasCodeContent && hasNonEmptyContent && codeContent.length > 0) {
        normalized.push(`\`\`\`${lang}`);
        normalized.push(...codeContent);
        normalized.push("```");
        i = j;
        continue;
      } else {
        // If no code content, just keep the language identifier as plain text
        normalized.push(line);
        i += 1;
        continue;
      }
    }

    // Default: keep line as-is
    normalized.push(line);
    i += 1;
  }

  return normalized.join("\n");
};

export const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({
  content,
  className,
  severity
}) => {
  // Filter out tool call delimiters and internal system messages that shouldn't be displayed
  const filteredContent = useMemo(() => {
    if (!content) return content;
    
    // Filter out tool call delimiters with special Unicode characters
    // Pattern matches: "Executing now:<｜tool▁calls▁begin｜><｜tool▁calls▁end｜>" and similar
    const toolCallDelimiterRegex = /Executing now:[<｜▁]*tool[▁_\s]*calls?[▁_\s]*begin[｜>]*[<｜▁]*[｜>]*[<｜▁]*tool[▁_\s]*calls?[▁_\s]*end[｜>]*/gi;
    
    // Also filter out standalone tool call delimiters
    const standaloneDelimiterRegex = /[<｜▁]*tool[▁_\s]*calls?[▁_\s]*(begin|end)[｜>]*/gi;
    
    let filtered = content.replace(toolCallDelimiterRegex, '');
    filtered = filtered.replace(standaloneDelimiterRegex, '');
    
    // Clean up any extra whitespace left behind
    filtered = filtered.replace(/\n\s*\n\s*\n/g, '\n\n').trim();
    
    return filtered;
  }, [content]);

  // Pre-process content to automatically fence detected code blocks
  const normalizedContent = useMemo(() => normalizeCodeBlocks(filteredContent), [filteredContent]);

  const baseClasses = cn(
    "prose prose-base max-w-none break-words font-sans",
    severity === "error"
      ? "text-red-600 dark:text-red-400 [&_p]:text-red-600 dark:[&_p]:text-red-400 [&_ul]:text-red-600 dark:[&_ul]:text-red-400 [&_ol]:text-red-600 dark:[&_ol]:text-red-400 [&_li]:text-red-600 dark:[&_li]:text-red-400 [&_h1]:text-red-600 dark:[&_h1]:text-red-400 [&_h2]:text-red-600 dark:[&_h2]:text-red-400 [&_h3]:text-red-600 dark:[&_h3]:text-red-400 [&_h4]:text-red-600 dark:[&_h4]:text-red-400 [&_h5]:text-red-600 dark:[&_h5]:text-red-400 [&_h6]:text-red-600 dark:[&_h6]:text-red-400 [&_strong]:text-red-600 dark:[&_strong]:text-red-400 [&_code]:text-red-600 dark:[&_code]:text-red-400 [&_blockquote]:text-red-600 dark:[&_blockquote]:text-red-400 [&_td]:text-red-600 dark:[&_td]:text-red-400 [&_th]:text-red-600 dark:[&_th]:text-red-400 [&_a]:text-red-600 dark:[&_a]:text-red-400 [&_a]:underline"
      : "text-foreground [&_p]:text-foreground [&_ul]:text-foreground [&_ol]:text-foreground [&_li]:text-foreground [&_h1]:text-foreground [&_h2]:text-foreground [&_h3]:text-foreground [&_h4]:text-foreground [&_h5]:text-foreground [&_h6]:text-foreground [&_strong]:text-foreground [&_code:not([class*='language-'])]:text-foreground [&_blockquote]:text-foreground [&_td]:text-foreground [&_th]:text-foreground [&_a]:text-green-400 dark:[&_a]:text-green-400 [&_a]:underline [&_a:hover]:text-green-300 dark:[&_a:hover]:text-green-300",
    "[&_ol]:list-decimal [&_ol]:ml-6 [&_ol]:pl-0 [&_ol>li]:pl-0 [&_ol>li]:ml-0 [&_ul]:list-disc [&_ul]:ml-6 [&_ul]:pl-0 [&_ul>li]:pl-0 [&_ul>li]:ml-0 [&_pre:not([class*='language-'])]:bg-gray-100 [&_pre:not([class*='language-'])]:text-gray-800 dark:[&_pre:not([class*='language-'])]:bg-zinc-900 dark:[&_pre:not([class*='language-'])]:text-gray-100",
    className
  );

  // Create the markdown with plugins
  const MarkdownWithPlugins = useMemo(() => {
    return (
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        rehypePlugins={[rehypeRaw]}
        className={baseClasses}
        components={components}
      >
        {normalizedContent}
      </ReactMarkdown>
    );
  }, [normalizedContent, baseClasses]);

  return MarkdownWithPlugins;
};

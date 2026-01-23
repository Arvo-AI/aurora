"use client";

import React, { useEffect, useRef, useState } from "react";
import { Card, CardContent } from "./card";
import { Button } from "./button";
import { Copy, Check, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";
import { getLanguageFromCode, getLanguageDisplayName } from "@/utils/language-detection";
// Import Prism.js with type assertion
import Prism from "prismjs";
import "prismjs/themes/prism-okaidia.css";
import "prismjs/components/prism-json";
import "prismjs/components/prism-yaml";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-python";
import "prismjs/components/prism-javascript";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-jsx";
import "prismjs/components/prism-tsx";
import "prismjs/components/prism-docker";
import "prismjs/components/prism-git";
import "prismjs/components/prism-markdown";
import "prismjs/components/prism-sql";

// Add HCL/Terraform support manually
Prism.languages.hcl = {
  'comment': [
    {
      pattern: /(^|[^\\])\/\*[\s\S]*?(?:\*\/|$)/,
      lookbehind: true,
      greedy: true
    },
    {
      pattern: /(^|[^\\:])\/\/.*/,
      lookbehind: true,
      greedy: true
    }
  ],
  'heredoc': {
    pattern: /<<-?\w+\s*[\r\n](?:.*[\r\n])*?[\s\S]*?[\r\n]\w+/,
    alias: 'string',
    greedy: true,
    inside: {
      'delimiter': {
        pattern: /^<<-?\w+|\w+$/,
        alias: 'variable'
      }
    }
  },
  'keyword': [
    {
      pattern: /(?:resource|data|variable|output|locals|module|provider|terraform|moved|import|check)\s+(?:"[^"]*"|`[^`]*`|\b\w+\b)/,
      lookbehind: true,
      inside: {
        'type': {
          pattern: /^(resource|data|variable|output|locals|module|provider|terraform|moved|import|check)/,
          alias: 'class-name'
        },
        'name': /"[^"]*"|`[^`]*`|\b\w+\b/
      }
    },
    /\b(?:resource|data|variable|output|locals|module|provider|terraform|moved|import|check|for|in|if|else|count|for_each|dynamic|content|precondition|postcondition|validation|lifecycle|depends_on|source|version|required_providers|required_version|backend|provisioner|connection|inline|file|remote_exec|local_exec|null_resource|random_id|random_string|random_pet|random_shuffle|random_integer|random_password|random_uuid|random_bytes|time_sleep|time_rotating|time_static|time_offset|timeadd|timecmp|formatdate|format|replace|split|join|concat|coalesce|coalescelist|compact|distinct|flatten|index|keys|length|list|lookup|map|matchkeys|merge|range|reverse|setintersection|setproduct|setsubtract|setunion|slice|sort|transpose|values|zipmap|base64decode|base64encode|base64gzip|base64sha256|base64sha512|bcrypt|ceil|chomp|chunklist|cidrhost|cidrnetmask|cidrsubnet|cidrsubnets|coalesce|coalescelist|compact|concat|contains|distinct|element|file|filebase64|filebase64sha256|filebase64sha512|fileexists|filemd5|filesha1|filesha256|filesha512|flatten|floor|format|formatdate|formatlist|indent|index|join|jsondecode|jsonencode|keys|length|list|log|lookup|lower|map|matchkeys|max|md5|merge|min|pathexpand|pow|range|regex|regexall|replace|reverse|rsadecrypt|sensitive|setintersection|setproduct|setsubtract|setunion|sha1|sha256|sha512|signum|slice|sort|split|strrev|substr|sum|textdecodebase64|textencodebase64|timeadd|timecmp|timestamp|title|transpose|trim|trimprefix|trimsuffix|try|upper|urlencode|uuid|uuidv5|values|yamldecode|yamlencode|zipmap)\b/
  ],
  'string': {
    pattern: /("|')(?:(?!\1)[^\\\r\n]|\\.)*\1/,
    greedy: true
  },
  'number': /\b0x[\da-f]+\b|\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b/i,
  'operator': /[=!<>]=?|\+|-|\*|\/|%|\|\||&&|\?|:|\.\.\./,
  'punctuation': /[{}[\];(),.]/
};

Prism.languages.terraform = Prism.languages.hcl;

interface CodeBlockProps {
  code: string;
  language?: string;
  className?: string;
  showLineNumbers?: boolean;
}





export const CodeBlock: React.FC<CodeBlockProps> = ({
  code,
  language: propLanguage,
  className,
  showLineNumbers = false
}) => {
  const [copied, setCopied] = useState(false);
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [language, setLanguage] = useState<string>('text');
  const [formattedCode, setFormattedCode] = useState<string>(code);
  const codeRef = useRef<HTMLElement>(null);

  useEffect(() => {
    // Determine language
    const detectedLanguage = propLanguage || getLanguageFromCode(code);
    setLanguage(detectedLanguage);
    
    // Auto-format JSON if language is json
    if (detectedLanguage === 'json') {
      try {
        // Try to parse and pretty-print the JSON
        const parsed = JSON.parse(code);
        const formatted = JSON.stringify(parsed, null, 2);
        setFormattedCode(formatted);
      } catch (e) {
        // If parsing fails, use the original code
        setFormattedCode(code);
      }
    } else {
      setFormattedCode(code);
    }
  }, [code, propLanguage]);

  useEffect(() => {
    // Apply syntax highlighting after language is set and component is mounted
    if (codeRef.current && language !== 'text') {
      // Use setTimeout to ensure DOM is ready
      setTimeout(() => {
        Prism.highlightElement(codeRef.current!);
      }, 0);
    }
  }, [language, formattedCode]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(formattedCode);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy code:', err);
    }
  };

  const languageDisplayName = getLanguageDisplayName(language);

  return (
    <Card className={cn("relative overflow-hidden", className)}>
      {/* Header with language label and buttons */}
      <div className="flex items-center justify-between px-4 py-2 bg-muted/50 border-b border-border">
        <span className="text-xs font-mono text-muted-foreground">
          {languageDisplayName}
        </span>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={handleCopy}
            className="h-6 w-6 p-0 hover:bg-muted"
          >
            {copied ? (
              <Check className="h-3 w-3 text-green-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
          </Button>
          <button
            onClick={() => setIsCollapsed(!isCollapsed)}
            aria-label={isCollapsed ? "Expand code" : "Collapse code"}
            className="flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors"
          >
            {isCollapsed ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronUp className="h-4 w-4" />
            )}
          </button>
        </div>
      </div>
      
      {/* Code content - collapsible */}
      {!isCollapsed && (
        <CardContent className="p-0">
          <pre className={cn("m-0 overflow-x-auto", `language-${language}`)}>
            <code
              ref={codeRef}
              className={cn(
                "block p-4",
                `language-${language}`,
                showLineNumbers && "line-numbers"
              )}
            >
              {formattedCode}
            </code>
          </pre>
        </CardContent>
      )}
    </Card>
  );
};

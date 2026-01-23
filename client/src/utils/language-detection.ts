export const getLanguageFromCode = (code: string): string => {
  // Normalize input
  const trimmedCode = (code || "").trim();

  // Bail early for empty strings
  if (trimmedCode === "") {
    return "text";
  }

  // Treat very short single-line snippets as text
  if (trimmedCode.length < 20 && !trimmedCode.includes("\n")) {
    return "text";
  }

  /**
   * Helper matchers ---------------------------------------------------------
   */
  const startsWithSheBang = /^#!\//.test(trimmedCode);
  const startsWithFrom    = /^FROM\s+\S+/i.test(trimmedCode);

  /** JSON ------------------------------------------------------------------*/
  if ((trimmedCode.startsWith("{") && trimmedCode.endsWith("}")) ||
      (trimmedCode.startsWith("[") && trimmedCode.endsWith("]"))) {
    try {
      JSON.parse(trimmedCode);
      return "json";
    } catch {
      // fall-through – not valid JSON, keep analysing
    }
  }

  /** YAML ------------------------------------------------------------------*/
  if (/^---/.test(trimmedCode) ||
      (/:/.test(trimmedCode) && /\n/.test(trimmedCode) && !/[{}]/.test(trimmedCode))) {
    return "yaml";
  }

  /** Dockerfile -------------------------------------------------------------*/
  if (startsWithFrom || /Dockerfile/i.test(trimmedCode)) {
    return "docker";
  }

  /** Bash / Shell -----------------------------------------------------------*/
  if (startsWithSheBang && /(bash|sh)/.test(trimmedCode)) {
    return "bash";
  }
  if (/\b(echo|cd|ls|mkdir|rm|cp|mv|grep|find|chmod|sudo)\b/.test(trimmedCode) && trimmedCode.includes("\n")) {
    return "bash";
  }

  /** Python ----------------------------------------------------------------*/
  if (/^(from\s+\w+\s+import|import\s+\w+|def\s+\w+|class\s+\w+)/.test(trimmedCode) ||
      /\bprint\(/.test(trimmedCode) ||
      /\bself\b/.test(trimmedCode)) {
    return "python";
  }

  /** TypeScript / JavaScript -----------------------------------------------*/
  if (/(interface\s+\w+|type\s+\w+|enum\s+\w+)/.test(trimmedCode)) {
    return "typescript"; // definitely TS
  }
  if (/(export\s+(default\s+)?|import\s+\w+|function\s+\w+|const\s+|let\s+|=>)/.test(trimmedCode)) {
    // A quick heuristic: presence of TS-specific annotations implies TS
    if (/:\s*\w+/.test(trimmedCode) || /<\w+>/.test(trimmedCode)) {
      return "typescript";
    }
    return "javascript";
  }

  /** HCL / Terraform --------------------------------------------------------*/
  if (/^(terraform|provider|resource|module|variable|output)\b/.test(trimmedCode) ||
      /\bresource\s+"[^"]+"\s+"[^"]+"/.test(trimmedCode)) {
    return "hcl";
  }
  if (/^(terraform|provider|resource|module|variable|output)\b/m.test(trimmedCode) ||
      /\bresource\s+"[^"]+"\s+"[^"]+"/.test(trimmedCode) ||
      /^\s*#.*\n.*\bresource\s+"[^"]+"\s+"[^"]+/.test(trimmedCode)) {
    return "hcl";
  }

  /** SQL --------------------------------------------------------------------*/
  if (/^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b/i.test(trimmedCode)) {
    return "sql";
  }

  // Default fallback --------------------------------------------------------
  return "text";
};

// Human-friendly display names – extend as required
export const getLanguageDisplayName = (language: string): string => {
  const languageMap: Record<string, string> = {
    json: "JSON",
    yaml: "YAML",
    yml: "YAML",
    bash: "Bash",
    shell: "Shell",
    python: "Python",
    javascript: "JavaScript",
    typescript: "TypeScript",
    jsx: "JSX",
    tsx: "TSX",
    docker: "Docker",
    git: "Git",
    markdown: "Markdown",
    hcl: "Terraform",
    terraform: "Terraform",
    sql: "SQL",
    text: "Text",
  };

  return languageMap[language] || language.toUpperCase();
};

/**
 * Command parsing utilities for tool execution display.
 * Extracts and formats commands from tool inputs for user-friendly display.
 * Extensible for future Pulumi support.
 */

// Helper: Provider-to-CLI mapping
const getProviderCli = (provider: string): string => {
  if (provider === 'gcp') return 'gcloud'
  if (provider === 'aws') return 'aws'
  if (provider === 'azure') return 'az'
  return ''
}

// Helper: Check if command already has recognized CLI prefix
const RECOGNIZED_CLI_REGEX = /^(gcloud|kubectl|gsutil|bq|aws|az)\b/i

export function extractIacPath(toolInput?: string): string | undefined {
  if (!toolInput) return undefined
  try {
    const parsed = JSON.parse(toolInput)
    if (parsed?.kwargs?.path && typeof parsed.kwargs.path === 'string') {
      return parsed.kwargs.path
    }
    if (parsed?.path && typeof parsed.path === 'string') {
      return parsed.path
    }
  } catch (error) {
    // Ignore JSON parse errors and try regex fallback
  }

  const regexMatch = toolInput.match(/"path"\s*:\s*"([^"\\]+)"/)
  if (regexMatch?.[1]) {
    return regexMatch[1]
  }

  const singleQuoteMatch = toolInput.match(/'path'\s*:\s*'([^'\\]+)'/)
  if (singleQuoteMatch?.[1]) {
    return singleQuoteMatch[1]
  }

  return undefined
}

export function extractIacAction(toolInput?: string, fallback?: string): string | undefined {
  if (!toolInput) return fallback
  try {
    const parsed = JSON.parse(toolInput.replace(/'/g, '"'))
    const action = parsed?.kwargs?.action ?? parsed?.action
    if (typeof action === 'string') {
      return action
    }
  } catch (error) {
    // Ignore parse errors and fall back to regex/detected value
  }

  const regexMatch = toolInput.match(/"action"\s*:\s*"([^"\\]+)"/)
  if (regexMatch?.[1]) {
    return regexMatch[1]
  }

  const singleQuoteMatch = toolInput.match(/'action'\s*:\s*'([^'\\]+)'/)
  if (singleQuoteMatch?.[1]) {
    return singleQuoteMatch[1]
  }

  return fallback
}

interface ParsedCloudExecCommand {
  command: string
  phase: 'input' | 'output'
}

export function parseCloudExecCommand(
  toolInput: string | undefined,
  toolOutput: any,
  defaultCommand: string
): ParsedCloudExecCommand {
  let command = defaultCommand

  // Phase 1: Extract from input (available immediately when tool starts)
  if (toolInput && !toolOutput) {
    try {
      const inputStr = typeof toolInput === 'string' ? toolInput : JSON.stringify(toolInput)
      const parsableInput = inputStr.replace(/'/g, '"')
      const parsed = JSON.parse(parsableInput)
      const inputCommand = parsed.command || parsed.kwargs?.command
      if (inputCommand && typeof inputCommand === 'string') {
        const provider = parsed.provider || parsed.kwargs?.provider

        if (provider) {
          const providerCli = getProviderCli(provider)
          // Backend sends raw command without prefix; add it for immediate display
          command = RECOGNIZED_CLI_REGEX.test(inputCommand.trim())
            ? inputCommand
            : providerCli ? `${providerCli} ${inputCommand}` : inputCommand
        } else {
          command = inputCommand
        }
      }
      return { command, phase: 'input' }
    } catch (error) {
      console.warn('[parseCloudExecCommand] Phase 1 parse failed:', {
        input_preview: typeof toolInput === 'string' ? toolInput.substring(0, 100) : '[object]',
        error: error instanceof Error ? error.message : String(error)
      })
    }
  }

  // Phase 2: Extract from output (available after completion, shows full command with flags)
  if (toolOutput) {
    try {
      const outputStr = typeof toolOutput === 'string' ? toolOutput : JSON.stringify(toolOutput)
      const parsed = JSON.parse(outputStr)
      if (parsed.final_command && typeof parsed.final_command === 'string') {
        return { command: parsed.final_command, phase: 'output' }
      }
    } catch (error) {
      console.warn('[parseCloudExecCommand] Phase 2 parse failed:', {
        output_preview: typeof toolOutput === 'string' ? toolOutput.substring(0, 100) : '[object]',
        error: error instanceof Error ? error.message : String(error)
      })
    }
  }

  return { command, phase: 'input' }
}

export function parseGitHubToolCommand(toolName: string, toolInput: string): string {
  try {
    // Replace single quotes with double quotes to handle Python's str() output
    const parsableCommand = toolInput.replace(/'/g, '"')
    const parsed = JSON.parse(parsableCommand)
    const args = parsed?.kwargs || parsed || {}

    const githubTool = toolName.replace("mcp_", "")

    switch(githubTool) {
      case "create_or_update_file":
        return `GitHub: Update ${args.path || "file"} in ${args.owner || ""}/${args.repo || ""}`
      case "search_repositories":
        return `GitHub: Search repos "${args.query || ""}"`
      case "list_repositories":
        return `GitHub: List repositories${args.type ? ` (${args.type})` : ""}`
      case "create_repository":
        return `GitHub: Create repo "${args.name || ""}"${args.private ? " (private)" : ""}`
      case "get_file_contents":
        return `GitHub: Get ${args.path || "file"} from ${args.owner || ""}/${args.repo || ""}`
      case "push_files":
        return `GitHub: Push files to ${args.owner || ""}/${args.repo || ""} (${args.branch || "main"})`
      case "create_issue":
        return `GitHub: Create issue in ${args.owner || ""}/${args.repo || ""}: "${args.title || ""}"`
      case "create_pull_request":
        return `GitHub: Create PR in ${args.owner || ""}/${args.repo || ""}: ${args.head || ""} → ${args.base || ""}`
      case "fork_repository":
        return `GitHub: Fork ${args.owner || ""}/${args.repo || ""}`
      case "create_branch":
        return `GitHub: Create branch "${args.branch || ""}" in ${args.owner || ""}/${args.repo || ""}`
      case "list_commits":
        return `GitHub: List commits in ${args.owner || ""}/${args.repo || ""}`
      case "list_issues":
        return `GitHub: List issues in ${args.owner || ""}/${args.repo || ""}`
      case "update_issue":
        return `GitHub: Update issue #${args.issue_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      case "add_issue_comment":
        return `GitHub: Comment on issue #${args.issue_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      case "search_code":
        return `GitHub: Search code "${args.q || args.query || ""}"`
      case "search_issues":
        return `GitHub: Search issues "${args.q || args.query || ""}"`
      case "search_users":
        return `GitHub: Search users "${args.q || args.query || ""}"`
      case "get_issue":
        return `GitHub: Get issue #${args.issue_number || ""} from ${args.owner || ""}/${args.repo || ""}`
      case "get_pull_request":
        return `GitHub: Get PR #${args.pull_number || ""} from ${args.owner || ""}/${args.repo || ""}`
      case "list_pull_requests":
        return `GitHub: List PRs in ${args.owner || ""}/${args.repo || ""}${args.state ? ` (${args.state})` : ""}`
      case "create_pull_request_review":
        return `GitHub: Review PR #${args.pull_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      case "merge_pull_request":
        return `GitHub: Merge PR #${args.pull_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      case "get_pull_request_files":
        return `GitHub: Get files from PR #${args.pull_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      case "get_pull_request_status":
        return `GitHub: Get status of PR #${args.pull_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      case "update_pull_request_branch":
        return `GitHub: Update PR #${args.pull_number || ""} branch in ${args.owner || ""}/${args.repo || ""}`
      case "get_pull_request_comments":
        return `GitHub: Get comments from PR #${args.pull_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      case "get_pull_request_reviews":
        return `GitHub: Get reviews from PR #${args.pull_number || ""} in ${args.owner || ""}/${args.repo || ""}`
      // Context7 MCP tools
      case "context7_get_library_docs":
        return `Context7: get-library-docs ${args.topic ? `"${args.topic}"` : args.context7CompatibleLibraryID || ""}`
      default:
        return `GitHub: ${githubTool.replace(/_/g, " ")}`
    }
  } catch (error) {
    const cleanName = toolName.replace("mcp_", "")
    return `GitHub: ${cleanName.replace(/_/g, " ")}`
  }
}

export function parseIacToolCommand(
  toolName: string,
  toolInput: string,
  iacAction: string | undefined
): string {
  try {
    const parsableCommand = toolInput.replace(/'/g, '"')
    const parsed = JSON.parse(parsableCommand)

    if (iacAction === 'write' && parsed && parsed.kwargs && parsed.kwargs.path) {
      const filename = parsed.kwargs.path.split('/').pop() || parsed.kwargs.path
      return filename
    } else if (iacAction === 'plan') {
      return "Terraform plan"
    } else if (iacAction === 'apply') {
      return "Terraform apply"
    }
  } catch (error) {
    // Keep existing command on parse failure
  }

  // Fallback based on action
  if (iacAction === 'plan') return "Terraform plan"
  if (iacAction === 'apply') return "Terraform apply"
  if (iacAction === 'write') return "Terraform write"
  
  return toolName.replace(/_/g, " ")
}

export function parseWebSearchCommand(toolInput: string): string {
  try {
    const parsed = JSON.parse(toolInput)
    const query = parsed?.kwargs?.query || parsed?.query
    if (query) {
      return `Search: ${query.length > 50 ? query.substring(0, 50) + '...' : query}`
    }
  } catch {
    // Ignore parse error
  }
  return "web search"
}

export function parseAwsMcpCommand(toolInput: string): string {
  try {
    const parsableCommand = toolInput.replace(/'/g, '"')
    const parsed = JSON.parse(parsableCommand)

    if (parsed && parsed.kwargs && parsed.kwargs.command) {
      return parsed.kwargs.command
    } else if (parsed && parsed.command) {
      return parsed.command
    }
  } catch (error) {
    // Keep existing command on parse failure
  }
  return "AWS command"
}

export function parseAwsSuggestCommand(toolInput: string): string {
  try {
    const parsableCommand = toolInput.replace(/'/g, '"')
    const parsed = JSON.parse(parsableCommand)

    if (parsed && parsed.kwargs && parsed.kwargs.query) {
      return `"${parsed.kwargs.query}"`
    } else if (parsed && parsed.query) {
      return `"${parsed.query}"`
    }
  } catch (error) {
    // Keep existing command on parse failure
  }
  return "Suggest AWS commands"
}

export function parseGitHubRcaCommand(toolInput: string): string {
  try {
    const parsableCommand = toolInput.replace(/'/g, '"')
    const parsed = JSON.parse(parsableCommand)
    const args = parsed?.kwargs || parsed || {}
    const action = args.action || "investigate"
    const repo = args.repo || ""

    switch(action) {
      case "deployment_check":
        return `GitHub: Check deployments${repo ? ` in ${repo}` : ""}`
      case "commits":
        return `GitHub: List recent commits${repo ? ` in ${repo}` : ""}`
      case "diff":
        const sha = args.commit_sha ? ` (${args.commit_sha.substring(0, 7)})` : ""
        return `GitHub: Get commit diff${sha}${repo ? ` in ${repo}` : ""}`
      case "pull_requests":
        return `GitHub: List merged PRs${repo ? ` in ${repo}` : ""}`
      default:
        return `GitHub: ${action.replace(/_/g, " ")}`
    }
  } catch (error) {
    return "GitHub: investigate"
  }
}

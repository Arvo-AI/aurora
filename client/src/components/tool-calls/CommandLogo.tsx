"use client"

import * as React from "react"
import { cn } from "@/lib/utils"
import { useTheme } from "next-themes"

interface CommandLogoProps {
  command?: string
  toolName?: string
  provider?: string
  className?: string
}

// Theme-aware GitHub logo component
const GitHubLogo: React.FC = () => {
  const { theme } = useTheme()
  const isDark = theme === 'dark'
  
  return (
    <img 
      src={isDark ? "/github-mark-white.png" : "/github-mark.svg"} 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="GitHub"
      onError={(e) => console.error('Failed to load GitHub logo:', e)}
    />
  )
}

// Logo components using actual logo files from public directory
const logos = {
  azure: (
    <img 
      src="/azure.ico" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="Azure CLI"
      onError={(e) => console.error('Failed to load Azure logo:', e)}
    />
  ),
  gcp: (
    <img 
      src="/google-cloud-svgrepo-com.svg" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="Google Cloud"
      onError={(e) => console.error('Failed to load GCP logo:', e)}
    />
  ),
  aws: (
    <img 
      src="/aws.ico" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="AWS CLI"
      onError={(e) => console.error('Failed to load AWS logo:', e)}
    />
  ),
  kubernetes: (
    <img 
      src="/kubernetes-svgrepo-com.svg" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="Kubernetes"
      onError={(e) => console.error('Failed to load Kubernetes logo:', e)}
    />
  ),
  terraform: (
    <img 
      src="/terraform-icon-svgrepo-com.svg" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="Terraform"
      onError={(e) => console.error('Failed to load Terraform logo:', e)}
    />
  ),
  docker: (
    <img 
      src="/docker-svgrepo-com.svg" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="Docker"
      onError={(e) => console.error('Failed to load Docker logo:', e)}
    />
  ),
  helm: (
    <img 
      src="/helm-svgrepo-com.svg" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="Helm"
      onError={(e) => console.error('Failed to load Helm logo:', e)}
    />
  ),
  git: (
    <img 
      src="/git-merge-svgrepo-com.svg" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="Git"
      onError={(e) => console.error('Failed to load Git logo:', e)}
    />
  ),
  ovh: (
    <img 
      src="/ovh.svg" 
      className="w-4 h-4 min-w-4 min-h-4 object-contain" 
      alt="OVH Cloud"
      onError={(e) => console.error('Failed to load OVH logo:', e)}
    />
  ),
  scaleway: (
    <img
      src="/scaleway.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="Scaleway"
      onError={(e) => console.error('Failed to load Scaleway logo:', e)}
    />
  ),
  tailscale: (
    <img
      src="/tailscale.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="Tailscale"
      onError={(e) => console.error('Failed to load Tailscale logo:', e)}
    />
  ),
  splunk: (
    <img
      src="/splunk.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="Splunk"
      onError={(e) => console.error('Failed to load Splunk logo:', e)}
    />
  ),
  jenkins: (
    <img
      src="/jenkins.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="Jenkins"
      onError={(e) => console.error('Failed to load Jenkins logo:', e)}
    />
  ),
  cloudbees: (
    <img
      src="/cloudbees.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="CloudBees"
      onError={(e) => console.error('Failed to load CloudBees logo:', e)}
    />
  ),
  coroot: (
    <img
      src="/coroot.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="Coroot"
      onError={(e) => console.error('Failed to load Coroot logo:', e)}
    />
  ),
  dynatrace: (
    <img
      src="/dynatrace.png"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="Dynatrace"
      onError={(e) => console.error('Failed to load Dynatrace logo:', e)}
    />
  ),
  thousandeyes: (
    <img
      src="/thousandeyes.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="ThousandEyes"
      onError={(e) => console.error('Failed to load ThousandEyes logo:', e)}
    />
  ),
  bitbucket: (
    <img
      src="/bitbucket.svg"
      className="w-4 h-4 min-w-4 min-h-4 object-contain"
      alt="Bitbucket"
      onError={(e) => console.error('Failed to load Bitbucket logo:', e)}
    />
  ),
  web: (
    <svg
      className="w-4 h-4 min-w-4 min-h-4 text-blue-600 dark:text-blue-400"
      fill="currentColor"
      viewBox="0 0 24 24"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.94-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
    </svg>
  ),
  knowledgeBase: (
    <svg
      className="w-4 h-4 min-w-4 min-h-4 text-purple-600 dark:text-purple-400"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      viewBox="0 0 24 24"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
    </svg>
  ),
  rcaUpdate: (
    <svg
      className="w-4 h-4 min-w-4 min-h-4 text-red-600 dark:text-red-400"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      viewBox="0 0 24 24"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 12a9 9 0 0 1 15.3-6.3M21 12a9 9 0 0 1-15.3 6.3" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 6v6h6M21 18v-6h-6" />
    </svg>
  ),
  github: <GitHubLogo />,
  default: (
    <span className="text-green-600 dark:text-green-400 select-none font-bold">$</span>
  )
}

const getLogoForCommand = (command: string | any, toolName: string, provider?: string): keyof typeof logos => {
  const cmd = (typeof command === 'string' ? command : String(command || '')).toLowerCase().trim()
  const tool = toolName.toLowerCase()
  const prov = (provider || '').toLowerCase()

  // Tailscale - check provider prop (cloud_exec passes provider)
  if (prov === 'tailscale' || tool.includes('tailscale') || cmd.includes('tailscale')) {
    return 'tailscale'
  }

  // Web search tool
  if (tool === 'web_search') {
    return 'web'
  }

  // Knowledge base tool
  if (tool === 'knowledge_base_search') {
    return 'knowledgeBase'
  }

  // RCA context updates
  if (tool === 'rca_context_update') {
    return 'rcaUpdate'
  }

  // Splunk tools
  if (tool.includes('splunk') || tool === 'search_splunk' || tool === 'list_splunk_indexes' || tool === 'list_splunk_sourcetypes') {
    return 'splunk'
  }

  // CloudBees tools
  if (tool.includes('cloudbees')) {
    return 'cloudbees'
  }

  // Jenkins tools
  if (tool.includes('jenkins')) {
    return 'jenkins'
  }

  // Coroot tools
  if (tool.includes('coroot')) {
    return 'coroot'
  }

  if (tool.includes('dynatrace') || tool === 'query_dynatrace') {
    return 'dynatrace'
  }

  // ThousandEyes tools
  if (tool.includes('thousandeyes')) {
    return 'thousandeyes'
  }

  // Bitbucket tools
  if (tool.startsWith('bitbucket_')) {
    return 'bitbucket'
  }


  // IAC tools
  if (tool.includes('iac') || tool.includes('terraform')) {
    return 'terraform'
  }

  // Cloud provider CLIs
  if (cmd.startsWith('az ') || cmd.includes('azure')) {
    return 'azure'
  }
  if (cmd.startsWith('gcloud ') || cmd.startsWith('gsutil ') || cmd.startsWith('bq ')) {
    return 'gcp'
  }
  if (cmd.startsWith('aws ')) {
    return 'aws'
  }
  if (cmd.startsWith('ovhcloud ') || cmd.includes('ovh')) {
    return 'ovh'
  }
  if (cmd.startsWith('scw ') || cmd.includes('scaleway')) {
    return 'scaleway'
  }

  // Container and orchestration tools
  if (cmd.startsWith('kubectl ') || cmd.includes('k8s')) {
    return 'kubernetes'
  }
  if (cmd.startsWith('docker ')) {
    return 'docker'
  }
  if (cmd.startsWith('helm ')) {
    return 'helm'
  }

  // Version control
  if (cmd.startsWith('git ')) {
    return 'git'
  }

  // AWS MCP tools - detect AWS MCP tools
  const isAwsMcpTool = (
    tool.includes('mcp_call_aws') ||
    tool.includes('mcp_suggest_aws_commands') ||
    tool.includes('call_aws') ||  // Fallback for old format
    tool.includes('suggest_aws_commands') ||  // Fallback for old format
    // Also check command for AWS-related keywords
    (cmd.includes('aws') && (tool.startsWith('mcp_') || cmd.includes('mcp')))
  )
  
  if (isAwsMcpTool) {
    return 'aws'
  }

  // GitHub MCP tools - detect with mcp_ prefix
  const isGithubMcpTool = (
    // Check for GitHub MCP tools with mcp_ prefix
    tool.startsWith('mcp_') && (
      tool.includes('search_repositories') ||
      tool.includes('create_repository') ||
      tool.includes('get_repository') ||
      tool.includes('list_repositories') ||
      tool.includes('get_file_contents') ||
      tool.includes('create_or_update_file') ||
      tool.includes('push_files') ||
      tool.includes('create_issue') ||
      tool.includes('get_issue') ||
      tool.includes('list_issues') ||
      tool.includes('update_issue') ||
      tool.includes('add_issue_comment') ||
      tool.includes('create_pull_request') ||
      tool.includes('list_pull_requests') ||
      tool.includes('get_pull_request') ||
      tool.includes('create_pull_request_review') ||
      tool.includes('merge_pull_request') ||
      tool.includes('get_pull_request_files') ||
      tool.includes('get_pull_request_status') ||
      tool.includes('update_pull_request_branch') ||
      tool.includes('get_pull_request_comments') ||
      tool.includes('get_pull_request_reviews') ||
      tool.includes('create_release') ||
      tool.includes('fork_repository') ||
      tool.includes('create_branch') ||
      tool.includes('list_commits') ||
      tool.includes('search_code') ||
      tool.includes('search_issues') ||
      tool.includes('search_users')
    ) ||
    tool === 'github' ||
    tool === 'github_rca' ||
    // Also check command for GitHub-related keywords
    cmd.toLowerCase().includes('github:')
  )
  
  if (isGithubMcpTool) {
    return 'github'
  }

  return 'default'
}

const CommandLogo: React.FC<CommandLogoProps> = ({
  command = '',
  toolName = '',
  provider = '',
  className
}) => {
  const logoKey = getLogoForCommand(command, toolName, provider)
  const logo = logos[logoKey]

  // Only apply color styling to the default $ symbol, let actual logos use their natural colors
  const logoClasses = logoKey === 'default' 
    ? "text-green-600 dark:text-green-400 select-none flex items-center"
    : "select-none flex items-center"

  return (
    <span className={cn(logoClasses, className)}>
      {logo}
    </span>
  )
}

export default CommandLogo
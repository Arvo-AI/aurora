'use client';

import { apiGet, apiPost, apiRequest, type ApiError } from '@/lib/services/api-client';

// ============================================================================
// Types
// Based on user research: minimal cognitive load, automatic analysis, 
// streaming thoughts, and copy-pasteable post-mortems
// ============================================================================

export type AlertSource = 'netdata' | 'datadog' | 'grafana' | 'prometheus' | 'pagerduty' | 'splunk' | 'dynatrace' | 'coroot' | 'bigpanda' | 'chat';
export type IncidentStatus = 'investigating' | 'analyzed' | 'merged' | 'resolved';
export type AuroraStatus = 'running' | 'summarizing' | 'complete' | 'error';
export type SuggestionRisk = 'safe' | 'low' | 'medium' | 'high';
export type SuggestionType = 'diagnostic' | 'mitigation' | 'communication' | 'fix';

export interface AlertMetadata {
  // Common fields
  alertUrl?: string;
  
  // Netdata specific
  chart?: string;
  context?: string;
  space?: string;
  room?: string;
  duration?: string;
  value?: string;
  additionalCriticalAlerts?: number;
  additionalWarningAlerts?: number;
  
  // Grafana specific
  dashboardUrl?: string;
  panelUrl?: string;
  labels?: Record<string, string>;
  summary?: string;
  description?: string;
  runbookUrl?: string;
  values?: Record<string, unknown>;
  imageUrl?: string;
  silenceUrl?: string;
  fingerprint?: string;
  
  // Datadog specific
  alertId?: string;
  metric?: string;
  query?: string;
  hostname?: string;
  tags?: string | string[];
  message?: string;
  priority?: string;
  snapshotUrl?: string;
  
  // PagerDuty specific
  incidentId?: string;
  incidentUrl?: string;
  urgency?: string;
  customFields?: Record<string, string>;
}

export interface Alert {
  source: AlertSource;
  sourceUrl: string;
  rawPayload: string;
  triggeredAt: string;
  title: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  service: string;
  metadata?: AlertMetadata;
}

export interface Suggestion {
  id: string;
  title: string;
  description: string;
  type: SuggestionType;
  risk: SuggestionRisk;
  command?: string; // Optional command to run
  // Execution tracking
  executedAt?: string;
  executionSessionId?: string;
  executionStatus?: 'in_progress' | 'completed' | 'failed' | 'executed';
  // Fix-type suggestion fields
  filePath?: string;
  originalContent?: string;
  suggestedContent?: string;
  userEditedContent?: string;
  repository?: string;
  prUrl?: string;
  prNumber?: number;
  createdBranch?: string;
  appliedAt?: string;
}

export interface PostMortem {
  title: string;
  incidentDate: string;
  duration: string;
  severity: string;
  timeline: string;
  impact: string;
  rootCause: string;
  remediation: string;
  actionItems: string[];
  lessonsLearned: string;
  attendees?: string[];
}

export interface PostmortemData {
  id: string;
  incidentId: string;
  content: string;  // markdown
  generatedAt: string;
  updatedAt: string;
  confluencePageId?: string;
  confluencePageUrl?: string;
  confluenceExportedAt?: string;
  notionPageId?: string;
  notionPageUrl?: string;
  notionExportedAt?: string;
  notionDatabaseId?: string;
}

export interface PostmortemListItem {
  id: string;
  incidentId: string;
  incidentTitle: string | null;
  content: string;
  generatedAt: string;
  updatedAt: string | null;
  confluencePageId: string | null;
  confluencePageUrl: string | null;
  confluenceExportedAt: string | null;
  notionPageId: string | null;
  notionPageUrl: string | null;
  notionExportedAt: string | null;
  notionDatabaseId: string | null;
}

export interface StreamingThought {
  id: string;
  timestamp: string;
  content: string;
  type: 'analysis' | 'finding' | 'hypothesis' | 'action';
}

export interface Citation {
  id: string;
  key: string;
  toolName: string;
  command: string;
  output: string;
  executedAt?: string;
  createdAt?: string;
}

export interface ChatSession {
  id: string;
  title: string;
  messages: Array<{
    id?: string;
    text?: string;
    content?: string;
    sender?: string;
    role?: string;
    type?: string;
  }>;
  status: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface CorrelatedAlert {
  id: string;
  sourceType: AlertSource;
  alertTitle: string;
  alertService: string;
  alertSeverity: string;
  correlationStrategy: string;
  correlationScore: number;
  correlationDetails: {
    topology?: number;
    time_window?: number;
    similarity?: number;
    correlated_alert_count?: number;
  };
  receivedAt: string;
}

export interface RecentIncident {
  id: string;
  alertTitle: string;
  alertService: string;
  severity: string;
  sourceType: AlertSource;
  status: IncidentStatus;
  auroraStatus: AuroraStatus;
  createdAt: string;
}

export interface Incident {
  id: string;
  alert: Alert;
  status: IncidentStatus;
  auroraStatus: AuroraStatus;
  summary: string; // THE MOST VALUABLE TEXT - what Aurora thinks is wrong
  streamingThoughts: StreamingThought[];
  suggestions: Suggestion[];
  citations?: Citation[]; // Evidence citations for the summary
  chatSessions?: ChatSession[]; // All chat sessions linked to this incident
  correlatedAlerts?: CorrelatedAlert[]; // Alerts correlated to this incident
  correlatedAlertCount?: number; // Count of correlated alerts (for list view)
  mergedIntoIncidentId?: string; // ID of incident this was merged into
  mergedIntoTitle?: string; // Title of incident this was merged into
  postMortem?: PostmortemData;
  startedAt: string;
  analyzedAt?: string;
  resolvedAt?: string;
  alertFiredAt?: string;
  createdAt?: string;
  updatedAt?: string;
  chatSessionId?: string; // RCA chat session ID
  activeTab?: 'thoughts' | 'chat'; // Currently active tab in the UI
  tokenUsage?: {
    requestCount: number;
    totalInputTokens: number;
    totalOutputTokens: number;
    totalTokens: number;
    totalCost: number;
    models?: {
      model: string;
      requestCount: number;
      inputTokens: number;
      outputTokens: number;
      cost: number;
    }[];
  } | null;
}

// Mock data removed - all data now comes from the backend API

// ============================================================================
// Service
// ============================================================================

export const incidentsService = {
  async getIncidents(): Promise<Incident[]> {
    try {
      const data = await apiGet<{ incidents: any[] }>('/api/incidents');

      return (data.incidents || []).map((inc: any) => ({
        id: inc.id,
        alert: {
          source: inc.sourceType as AlertSource,
          sourceUrl: inc.alert?.sourceUrl || '',
          rawPayload: '',
          // Prefer the actual alert fire time so the UI matches MTTD math.
          triggeredAt: inc.alertFiredAt ?? inc.startedAt,
          title: inc.alert.title,
          severity: inc.severity,
          service: inc.alert.service,
        },
        status: inc.status as IncidentStatus,
        auroraStatus: (inc.auroraStatus || 'idle') as AuroraStatus,
        summary: inc.summary || '',
        streamingThoughts: inc.streamingThoughts || [],
        suggestions: inc.suggestions || [],
        correlatedAlertCount: inc.correlatedAlertCount || 0,
        mergedIntoIncidentId: inc.mergedIntoIncidentId,
        mergedIntoTitle: inc.mergedIntoTitle,
        postMortem: inc.postMortem ?? undefined,
        startedAt: inc.startedAt,
        analyzedAt: inc.analyzedAt,
        resolvedAt: inc.resolvedAt,
        alertFiredAt: inc.alertFiredAt,
        createdAt: inc.createdAt,
        updatedAt: inc.updatedAt,
        activeTab: inc.activeTab || 'thoughts',
      }));
    } catch (error) {
      console.error('Error fetching incidents:', error);
      return [];
    }
  },

  async getIncident(id: string): Promise<Incident | null> {
    try {
      const data = await apiGet<{ incident: any }>(`/api/incidents/${id}`);
      const inc = data.incident;

      return {
        id: inc.id,
        alert: {
          source: inc.sourceType as AlertSource,
          sourceUrl: inc.alert?.sourceUrl || '',
          rawPayload: inc.alert?.rawPayload || '',
          triggeredAt: inc.alertFiredAt ?? inc.startedAt,
          title: inc.alert?.title || '',
          severity: inc.severity,
          service: inc.alert?.service || 'unknown',
          metadata: inc.alert?.metadata || undefined,
        },
        status: inc.status as IncidentStatus,
        auroraStatus: (inc.auroraStatus || 'idle') as AuroraStatus,
        summary: inc.summary || '',
        streamingThoughts: (inc.streamingThoughts || []).map((t: any) => ({
          id: t.id,
          timestamp: t.timestamp,
          content: t.content,
          type: t.type || 'analysis',
        })),
        suggestions: (inc.suggestions || []).map((s: any) => ({
          id: s.id,
          title: s.title,
          description: s.description,
          type: s.type || 'diagnostic',
          risk: s.risk || 'safe',
          command: s.command,
          filePath: s.filePath,
          originalContent: s.originalContent,
          suggestedContent: s.suggestedContent,
          userEditedContent: s.userEditedContent,
          repository: s.repository,
          prUrl: s.prUrl,
          prNumber: s.prNumber,
          createdBranch: s.createdBranch,
          appliedAt: s.appliedAt,
          executedAt: s.executedAt,
          executionSessionId: s.executionSessionId,
          executionStatus: s.executionStatus,
        })),
        citations: (inc.citations || []).map((c: any) => ({
          id: c.id,
          key: c.key,
          toolName: c.toolName,
          command: c.command,
          output: c.output,
          executedAt: c.executedAt,
          createdAt: c.createdAt,
        })),
        chatSessions: (inc.chatSessions || []).map((cs: any) => ({
          id: cs.id,
          title: cs.title,
          messages: cs.messages || [],
          status: cs.status || 'active',
          createdAt: cs.createdAt,
          updatedAt: cs.updatedAt,
        })),
        correlatedAlerts: (inc.correlatedAlerts || []).map((ca: any) => ({
          id: ca.id,
          sourceType: ca.sourceType as AlertSource,
          alertTitle: ca.alertTitle,
          alertService: ca.alertService,
          alertSeverity: ca.alertSeverity,
          correlationStrategy: ca.correlationStrategy,
          correlationScore: ca.correlationScore,
          correlationDetails: ca.correlationDetails || {},
          receivedAt: ca.receivedAt,
        })),
        mergedIntoIncidentId: inc.mergedIntoIncidentId,
        mergedIntoTitle: inc.mergedIntoTitle,
        postMortem: inc.postMortem ?? undefined,
        startedAt: inc.startedAt,
        analyzedAt: inc.analyzedAt,
        resolvedAt: inc.resolvedAt,
        alertFiredAt: inc.alertFiredAt,
        createdAt: inc.createdAt,
        updatedAt: inc.updatedAt,
        chatSessionId: inc.chatSessionId,
        activeTab: inc.activeTab || 'thoughts',
        tokenUsage: inc.tokenUsage || null,
      };
    } catch (error) {
      if ((error as ApiError).status === 404) {
        return null;
      }
      console.error('Error fetching incident:', error);
      return null;
    }
  },

  async getActiveCount(): Promise<number> {
    try {
      const incidents = await this.getIncidents();
      return incidents.filter(i => i.auroraStatus === 'running' || i.auroraStatus === 'summarizing').length;
    } catch (error) {
      console.error('Error getting active count:', error);
      return 0;
    }
  },

  async updateActiveTab(incidentId: string, activeTab: 'thoughts' | 'chat'): Promise<void> {
    try {
      await apiRequest(`/api/incidents/${incidentId}`, {
        method: 'PATCH',
        body: JSON.stringify({ activeTab }),
      });
    } catch (error) {
      console.error('Error updating active tab:', error);
    }
  },

  formatDuration(startTime: string): string {
    const start = new Date(startTime).getTime();
    const end = Date.now();
    const diffMs = end - start;
    const diffMins = Math.floor(diffMs / 60000);
    const hours = Math.floor(diffMins / 60);
    const days = Math.floor(hours / 24);
    const mins = diffMins % 60;
    
    if (days > 0) {
      const remainingHours = hours % 24;
      if (remainingHours > 0) {
        return `${days}d ${remainingHours}h ${mins}m`;
      }
      return `${days}d ${mins}m`;
    }
    if (hours > 0) return `${hours}h ${mins}m`;
    return `${mins}m`;
  },

  formatTimeAgo(timestamp: string): string {
    const diffMs = Date.now() - new Date(timestamp).getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const hours = Math.floor(diffMins / 60);
    const days = Math.floor(hours / 24);
    
    if (days > 0) {
      const remainingHours = hours % 24;
      return remainingHours > 0 ? `${days}d ${remainingHours}h ago` : `${days}d ago`;
    }
    if (hours > 0) return `${hours}h ${diffMins % 60}m ago`;
    return `${diffMins}m ago`;
  },

  getSeverityColor(severity: string): string {
    switch (severity) {
      case 'critical': return 'bg-red-600 text-white';
      case 'high': return 'bg-orange-500 text-white';
      case 'medium': return 'bg-yellow-500 text-black';
      case 'low': return 'bg-blue-500 text-white';
      default: return 'bg-gray-500 text-white';
    }
  },

  getStatusColor(status: IncidentStatus): string {
    switch (status) {
      case 'investigating': return 'text-orange-500';
      case 'analyzed': return 'text-blue-500';
      case 'resolved': return 'text-green-500';
      case 'merged': return 'text-zinc-500';
    }
  },

  getAuroraStatusLabel(status: AuroraStatus): string {
    switch (status) {
      case 'running': return 'Aurora Investigating...';
      case 'summarizing': return 'Generating Summary...';
      case 'complete': return 'Analysis Complete';
      case 'error': return 'Analysis Error';
    }
  },

  getRiskColor(risk: SuggestionRisk): string {
    switch (risk) {
      case 'safe': return 'bg-green-500/20 text-green-400 border-green-500/30';
      case 'low': return 'bg-blue-500/20 text-blue-400 border-blue-500/30';
      case 'medium': return 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30';
      case 'high': return 'bg-red-500/20 text-red-400 border-red-500/30';
    }
  },

  async updateFixSuggestion(suggestionId: string, userEditedContent: string): Promise<{ success: boolean }> {
    return apiRequest<{ success: boolean }>(`/api/incidents/suggestions/${suggestionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ userEditedContent }),
    });
  },

  async applyFixSuggestion(
    suggestionId: string,
    options?: { useEditedContent?: boolean; targetBranch?: string }
  ): Promise<{ success: boolean; prUrl?: string; prNumber?: number; error?: string }> {
    try {
      return await apiPost<{ success: boolean; prUrl?: string; prNumber?: number }>(
        `/api/incidents/suggestions/${suggestionId}/apply`,
        {
          useEditedContent: options?.useEditedContent ?? true,
          targetBranch: options?.targetBranch,
        },
      );
    } catch (error) {
      console.error('Error applying fix suggestion:', error);
      const message = error instanceof Error ? error.message : 'Unknown error';
      return { success: false, error: message };
    }
  },

  async getRecentUnlinkedIncidents(excludeId?: string): Promise<RecentIncident[]> {
    try {
      const url = excludeId 
        ? `/api/incidents/recent-unlinked?exclude=${encodeURIComponent(excludeId)}`
        : '/api/incidents/recent-unlinked';

      const data = await apiGet<{ incidents: RecentIncident[] }>(url);
      return data.incidents || [];
    } catch (error) {
      console.error('Error fetching recent unlinked incidents:', error);
      return [];
    }
  },

  async mergeAlertToIncident(
    targetIncidentId: string,
    sourceIncidentId: string
  ): Promise<{ success: boolean; error?: string }> {
    try {
      await apiPost(`/api/incidents/${targetIncidentId}/merge-alert`, { sourceIncidentId });
      return { success: true };
    } catch (error) {
      console.error('Error merging alert:', error);
      const message = error instanceof Error ? error.message : 'Unknown error';
      return { success: false, error: message };
    }
  },

  async resolveIncident(incidentId: string): Promise<void> {
    await apiRequest(`/api/incidents/${incidentId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'resolved' }),
    });
  },
};

// ============================================================================
// Postmortem Service
// ============================================================================

export const postmortemService = {
  async getPostmortem(incidentId: string): Promise<{ data: PostmortemData | null; error?: string }> {
    try {
      const data = await apiGet<{ postmortem: PostmortemData | null }>(`/api/incidents/${incidentId}/postmortem`);
      return { data: data.postmortem || null };
    } catch (error) {
      if ((error as ApiError).status === 404) {
        return { data: null };
      }
      console.error('Error fetching postmortem:', error);
      return { data: null, error: error instanceof Error ? error.message : 'Network error' };
    }
  },

  async updatePostmortem(incidentId: string, content: string): Promise<{ success: boolean }> {
    try {
      await apiRequest(`/api/incidents/${incidentId}/postmortem`, {
        method: 'PATCH',
        body: JSON.stringify({ content }),
      });
      return { success: true };
    } catch (error) {
      console.error('Error updating postmortem:', error);
      return { success: false };
    }
  },

  async exportToConfluence(
    incidentId: string,
    spaceKey: string,
    parentPageId?: string
  ): Promise<{ success: boolean; pageUrl?: string; error?: string }> {
    try {
      return await apiPost<{ success: boolean; pageUrl?: string }>(
        `/api/incidents/${incidentId}/postmortem/export/confluence`,
        { spaceKey, parentPageId },
      );
    } catch (error) {
      console.error('Error exporting to Confluence:', error);
      const message = error instanceof Error ? error.message : 'Unknown error';
      return { success: false, error: message };
    }
  },

  async exportToNotion(
    incidentId: string,
    params: {
      databaseId: string;
      titleProperty?: string;
      propertyMapping?: Record<string, string>;
      actionItemsDatabaseId?: string;
    },
  ): Promise<{ success: boolean; pageUrl?: string; pageId?: string; actionItemCount?: number; error?: string; code?: string }> {
    try {
      const response = await fetch(`/api/incidents/${incidentId}/postmortem/export/notion`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          databaseId: params.databaseId,
          titleProperty: params.titleProperty,
          propertyMapping: params.propertyMapping,
          actionItemsDatabaseId: params.actionItemsDatabaseId,
        }),
      });
      const text = await response.text();
      let data: Record<string, unknown> = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch {
        data = {};
      }
      if (!response.ok) {
        return {
          success: false,
          error: (data.error as string) || `Export failed (${response.status})`,
          code: data.code as string | undefined,
        };
      }
      return {
        success: Boolean(data.success ?? true),
        pageUrl: data.pageUrl as string | undefined,
        pageId: data.pageId as string | undefined,
        actionItemCount: data.actionItemCount as number | undefined,
      };
    } catch (error) {
      console.error('Error exporting to Notion:', error);
      const message = error instanceof Error ? error.message : 'Unknown error';
      return { success: false, error: message };
    }
  },

  downloadMarkdown(incidentId: string, content: string, title: string): void {
    const blob = new Blob([content], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `postmortem-${title.replace(/[^a-z0-9]/gi, '-').toLowerCase()}.md`;
    a.click();
    URL.revokeObjectURL(url);
  },

  async listPostmortems(): Promise<PostmortemListItem[]> {
    try {
      const data = await apiGet<{ postmortems: PostmortemListItem[] }>('/api/postmortems');
      return data.postmortems ?? [];
    } catch (error) {
      console.error('Error fetching postmortems:', error);
      return [];
    }
  },
};


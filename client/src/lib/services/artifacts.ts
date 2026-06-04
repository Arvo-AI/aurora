'use client';

import { apiGet, apiPost, apiRequest, type ApiError } from '@/lib/services/api-client';

// ============================================================================
// Types
// ============================================================================

export type ArtifactEditor = 'agent' | 'user';

export interface ArtifactSummary {
  id: string;
  title: string;
  version: number;
  lastEditedBy: ArtifactEditor;
  updatedAt: string | null;
  createdAt: string | null;
}

export interface ArtifactData extends ArtifactSummary {
  content: string;
}

export interface ArtifactVersion {
  id: string;
  versionNumber: number;
  source: string;
  userId: string;
  createdAt: string | null;
  generationSessionId: string | null;
}

export interface ArtifactVersionDetail extends ArtifactVersion {
  content: string;
}

// ============================================================================
// Service
// ============================================================================

export const artifactsService = {
  async listArtifacts(): Promise<ArtifactSummary[]> {
    try {
      const data = await apiGet<{ artifacts: ArtifactSummary[] }>('/api/artifacts');
      return data.artifacts ?? [];
    } catch (error) {
      console.error('Error fetching artifacts:', error);
      return [];
    }
  },

  async getArtifact(id: string): Promise<ArtifactData | null> {
    try {
      const data = await apiGet<{ artifact: ArtifactData }>(`/api/artifacts/${id}`);
      return data.artifact ?? null;
    } catch (error) {
      if ((error as ApiError).status === 404) return null;
      console.error('Error fetching artifact:', error);
      return null;
    }
  },

  async createArtifact(title: string, content: string): Promise<{ success: boolean; id?: string; error?: string }> {
    try {
      const data = await apiPost<{ id: string; version: number }>('/api/artifacts', { title, content });
      return { success: true, id: data.id };
    } catch (error) {
      const apiErr = error as ApiError;
      return { success: false, error: apiErr.message || 'Failed to create artifact' };
    }
  },

  async updateArtifact(id: string, content: string): Promise<{ success: boolean; error?: string }> {
    try {
      await apiRequest(`/api/artifacts/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ content }),
      });
      return { success: true };
    } catch (error) {
      const apiErr = error as ApiError;
      return { success: false, error: apiErr.message || 'Failed to update artifact' };
    }
  },

  async deleteArtifact(id: string): Promise<{ success: boolean; error?: string }> {
    try {
      await apiRequest(`/api/artifacts/${id}`, { method: 'DELETE' });
      return { success: true };
    } catch (error) {
      const apiErr = error as ApiError;
      return { success: false, error: apiErr.message || 'Failed to delete artifact' };
    }
  },

  async getVersions(id: string): Promise<{ versions: ArtifactVersion[]; currentVersionId: string | null; error?: string }> {
    try {
      const data = await apiGet<{ versions: ArtifactVersion[]; currentVersionId: string | null }>(
        `/api/artifacts/${id}/versions`,
      );
      return { versions: data.versions ?? [], currentVersionId: data.currentVersionId ?? null };
    } catch (error) {
      const apiErr = error as ApiError;
      return { versions: [], currentVersionId: null, error: apiErr.message || 'Failed to load versions' };
    }
  },

  async getVersion(id: string, versionId: string): Promise<ArtifactVersionDetail | null> {
    try {
      const data = await apiGet<{ version: ArtifactVersionDetail }>(
        `/api/artifacts/${id}/versions/${versionId}`,
      );
      return data.version ?? null;
    } catch (error) {
      console.error('Error fetching artifact version:', error);
      return null;
    }
  },

  async restoreVersion(id: string, versionId: string): Promise<{ success: boolean; content?: string; error?: string }> {
    try {
      const data = await apiPost<{ success: boolean; content: string }>(
        `/api/artifacts/${id}/versions/${versionId}/restore`,
      );
      return { success: true, content: data.content };
    } catch (error) {
      const apiErr = error as ApiError;
      return { success: false, error: apiErr.message || 'Failed to restore version' };
    }
  },
};

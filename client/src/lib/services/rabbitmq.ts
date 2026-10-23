'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface RabbitMQStatus {
  connected: boolean;
  url?: string;
  username?: string;
  vhost?: string;
  clusterName?: string;
  version?: string;
  tags?: string[];
  verifyTls?: boolean;
  error?: string;
  validatedAt?: string;
}

export interface RabbitMQConnectPayload {
  url: string;
  username: string;
  password: string;
  vhost?: string;
  verify_tls: boolean;
}

export interface RabbitMQConnectResponse {
  success: boolean;
  url: string;
  username: string;
  vhost: string;
  clusterName?: string;
  version?: string;
  tags?: string[];
  verifyTls?: boolean;
}

const API_BASE = '/api/rabbitmq';

function asStatus(data: UnknownRecord | null): RabbitMQStatus | null {
  if (!data) return null;
  return {
    connected: Boolean(data.connected),
    url: data.url as string | undefined,
    username: data.username as string | undefined,
    vhost: data.vhost as string | undefined,
    clusterName: (data.clusterName ?? data.cluster_name) as string | undefined,
    version: (data.version ?? data.rabbitmq_version) as string | undefined,
    tags: Array.isArray(data.tags) ? (data.tags as string[]) : [],
    verifyTls: (data.verifyTls ?? data.verify_tls) as boolean | undefined,
    error: data.error as string | undefined,
    validatedAt: (data.validatedAt ?? data.validated_at) as string | undefined,
  };
}

export const rabbitmqService = {
  async getStatus(): Promise<RabbitMQStatus | null> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, { cache: 'no-store' });
    return asStatus(data);
  },

  async connect(payload: RabbitMQConnectPayload): Promise<RabbitMQConnectResponse> {
    return apiRequest<RabbitMQConnectResponse>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
      timeout: 30_000,
    });
  },
};

'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface RabbitMQAccount {
  accountId: string;
  name?: string;
  displayName?: string;
  url?: string;
  username?: string;
  vhost?: string;
  clusterName?: string;
  version?: string;
  tags?: string[];
  validatedAt?: string;
  allowed?: boolean;
  valid?: boolean;
  error?: string;
}

export interface RabbitMQStatus {
  connected: boolean;
  accounts?: RabbitMQAccount[];
  url?: string;
  username?: string;
  vhost?: string;
  clusterName?: string;
  version?: string;
  tags?: string[];
  error?: string;
  validatedAt?: string;
}

export interface RabbitMQConnectPayload {
  url: string;
  username: string;
  password: string;
  vhost?: string;
  name?: string;
}

export interface RabbitMQConnectResponse extends RabbitMQStatus {
  success: boolean;
  replaced?: boolean;
  accountId?: string;
  connectedAccount?: RabbitMQAccount | null;
}

const API_BASE = '/api/rabbitmq';

function asAccount(raw: unknown): RabbitMQAccount | null {
  if (!raw || typeof raw !== 'object') return null;
  const data = raw as UnknownRecord;
  if (typeof data.accountId !== 'string' || !data.accountId) return null;
  return {
    accountId: data.accountId,
    name: (data.name as string | null) ?? undefined,
    displayName: data.displayName as string | undefined,
    url: data.url as string | undefined,
    username: data.username as string | undefined,
    vhost: data.vhost as string | undefined,
    clusterName: data.clusterName as string | undefined,
    version: data.version as string | undefined,
    tags: Array.isArray(data.tags) ? (data.tags as string[]) : [],
    validatedAt: data.validatedAt as string | undefined,
    allowed: data.allowed as boolean | undefined,
    valid: data.valid as boolean | undefined,
    error: data.error as string | undefined,
  };
}

/** Every broker. A status cached before brokers were listed still describes one. */
export function rabbitmqAccounts(status: RabbitMQStatus | null): RabbitMQAccount[] {
  if (!status?.connected) return [];
  if (status.accounts && status.accounts.length > 0) return status.accounts;
  if (!status.url) return [];
  return [{
    accountId: status.url,
    url: status.url,
    username: status.username,
    vhost: status.vhost,
    clusterName: status.clusterName,
    version: status.version,
    tags: status.tags,
    validatedAt: status.validatedAt,
    error: status.error,
  }];
}

function asStatus(data: UnknownRecord | null): RabbitMQStatus | null {
  if (!data) return null;
  const accounts = Array.isArray(data.accounts)
    ? data.accounts.map(asAccount).filter((a): a is RabbitMQAccount => a !== null)
    : undefined;
  return {
    connected: Boolean(data.connected),
    accounts,
    url: data.url as string | undefined,
    username: data.username as string | undefined,
    vhost: data.vhost as string | undefined,
    clusterName: (data.clusterName ?? data.cluster_name) as string | undefined,
    version: (data.version ?? data.rabbitmq_version) as string | undefined,
    tags: Array.isArray(data.tags) ? (data.tags as string[]) : [],
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
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
      timeout: 30_000,
    });
    const status = asStatus(data) ?? { connected: true };
    return {
      ...status,
      success: Boolean(data?.success),
      replaced: Boolean(data?.replaced),
      accountId: data?.accountId as string | undefined,
      connectedAccount: asAccount(data?.connectedAccount),
    };
  },

  /** Remove one broker by account id, or every broker when none is given. */
  async disconnect(accountId?: string): Promise<void> {
    const qs = accountId ? `?account=${encodeURIComponent(accountId)}` : '';
    await apiRequest<UnknownRecord>(`/api/connected-accounts/rabbitmq${qs}`, {
      method: 'DELETE',
      cache: 'no-store',
      // A retried remove could hit the next broker's response; fail once instead.
      retries: 0,
    });
  },
};

"use client";

import React from "react";
import Link from "next/link";
import StatusBadge from "./StatusBadge";
import { formatTimeAgo } from "@/lib/utils/time-format";
import type { Resource } from "@/lib/services/observability";

interface Props {
  resources: Resource[];
  isLoading: boolean;
  page: number;
  totalPages: number;
  total: number;
  onPageChange: (page: number) => void;
}

function formatType(resource: Resource): string {
  const sub = resource.sub_type;
  const rt = resource.resource_type;
  if (sub && sub !== rt) {
    return `${rt} (${sub})`;
  }
  return rt;
}

function LoadingSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="animate-pulse flex items-center gap-4 px-4 py-3">
          <div className="h-3 w-3 rounded-full bg-muted" />
          <div className="h-4 w-48 bg-muted rounded" />
          <div className="h-4 w-32 bg-muted rounded" />
          <div className="h-4 w-16 bg-muted rounded" />
          <div className="h-4 w-20 bg-muted rounded" />
          <div className="h-4 w-16 bg-muted rounded" />
        </div>
      ))}
    </div>
  );
}

export default function ResourceTable({ resources, isLoading, page, totalPages, total, onPageChange }: Props) {
  if (isLoading) return <LoadingSkeleton />;

  if (!resources.length) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        <p className="text-lg font-medium">No resources found</p>
        <p className="text-sm mt-1">Connect a cloud provider and run discovery to see your infrastructure.</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-card shadow-sm overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/50">
              <th className="text-left px-4 py-3 font-medium text-muted-foreground w-8"></th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">Name</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">Type</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">Provider</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">Region</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">Updated</th>
            </tr>
          </thead>
          <tbody>
            {resources.map((resource) => (
              <tr key={resource.id} className="border-b border-border/50 hover:bg-muted/30 transition-colors">
                <td className="px-4 py-3">
                  <StatusBadge status={resource.status} />
                </td>
                <td className="px-4 py-3">
                  <Link
                    href={`/observability/${encodeURIComponent(resource.name)}`}
                    className="font-medium text-foreground hover:text-primary hover:underline"
                  >
                    {resource.display_name || resource.name}
                  </Link>
                </td>
                <td className="px-4 py-3 text-muted-foreground">{formatType(resource)}</td>
                <td className="px-4 py-3">
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-muted text-muted-foreground capitalize">
                    {resource.provider}
                  </span>
                </td>
                <td className="px-4 py-3 text-muted-foreground">{resource.region || "-"}</td>
                <td className="px-4 py-3 text-muted-foreground">{formatTimeAgo(resource.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between px-4 py-3 border-t border-border">
          <span className="text-sm text-muted-foreground">
            Page {page} of {totalPages} ({total} resources)
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              className="px-3 py-1 text-sm rounded-md border border-border hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages}
              className="px-3 py-1 text-sm rounded-md border border-border hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Rocket } from "lucide-react";
import { formatTimeAgo } from "@/lib/utils/time-format";
import type { SpinnakerDeploymentEvent } from "@/lib/services/spinnaker";

interface DeploymentsListProps {
  deployments: SpinnakerDeploymentEvent[];
}

export function DeploymentsList({ deployments }: DeploymentsListProps) {
  if (deployments.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Rocket className="h-5 w-5 text-teal-600" />
          <CardTitle className="text-lg">Recent Deployments</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          {deployments.map((dep) => {
            const timeAgo = dep.receivedAt ? formatTimeAgo(new Date(dep.receivedAt)) : null;
            return (
              <div key={dep.id} className="flex items-start justify-between p-3 rounded-lg border bg-card hover:bg-muted/50 transition-colors">
                <div className="flex-1 min-w-0 space-y-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge
                      variant={
                        dep.status === "SUCCEEDED" ? "default" :
                        dep.status === "TERMINAL" || dep.status === "FAILED_CONTINUE" ? "destructive" :
                        "secondary"
                      }
                      className="h-5 text-xs shrink-0"
                    >
                      {dep.status}
                    </Badge>
                    <span className="font-medium text-sm">{dep.application}</span>
                  </div>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
                    <span>{dep.pipelineName}</span>
                    {dep.triggerType && (
                      <>
                        <span>&bull;</span>
                        <span>{dep.triggerType}</span>
                      </>
                    )}
                    {timeAgo && (
                      <>
                        <span>&bull;</span>
                        <span>{timeAgo}</span>
                      </>
                    )}
                    {dep.triggerUser && dep.triggerUser !== "anonymous" && (
                      <>
                        <span>&bull;</span>
                        <span>by {dep.triggerUser}</span>
                      </>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

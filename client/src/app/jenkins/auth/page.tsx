"use client";

import CIProviderAuthPage from "@/components/ci-provider-auth-page";
import { jenkinsConfig } from "@/lib/services/ci-provider-configs";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

export default function JenkinsAuthPage() {
  return (
    <ConnectorAuthGuard connectorName="Jenkins">
      <CIProviderAuthPage config={jenkinsConfig} />
    </ConnectorAuthGuard>
  );
}

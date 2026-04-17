"use client";

import CIProviderAuthPage from "@/components/ci-provider-auth-page";
import { codefreshConfig } from "@/lib/services/ci-provider-configs";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

export default function CodefreshAuthPage() {
  return (
    <ConnectorAuthGuard connectorName="Codefresh">
      <CIProviderAuthPage config={codefreshConfig} />
    </ConnectorAuthGuard>
  );
}

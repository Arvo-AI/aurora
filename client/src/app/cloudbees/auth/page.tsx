"use client";

import CIProviderAuthPage from "@/components/ci-provider-auth-page";
import { cloudbeesConfig } from "@/lib/services/ci-provider-configs";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

export default function CloudBeesAuthPage() {
  return (
    <ConnectorAuthGuard connectorName="CloudBees">
      <CIProviderAuthPage config={cloudbeesConfig} />
    </ConnectorAuthGuard>
  );
}

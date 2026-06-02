"use client";

import CloudBeesAuthPage from "./components/CloudBeesAuthPage";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

export default function CloudBeesAuthPageWrapper() {
  return (
    <ConnectorAuthGuard connectorName="CloudBees">
      <CloudBeesAuthPage />
    </ConnectorAuthGuard>
  );
}

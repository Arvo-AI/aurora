"use client";

import { SSHKeyManager } from "@/components/SSHKeyManager";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

export default function SshKeysPage() {
  return (
    <ConnectorAuthGuard connectorName="SSH Keys">
      <div className="mx-auto max-w-6xl px-6 py-10">
        <SSHKeyManager />
      </div>
    </ConnectorAuthGuard>
  );
}

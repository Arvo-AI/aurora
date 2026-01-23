"use client";

import { SSHKeyManager } from "@/components/SSHKeyManager";

export default function SshKeysPage() {
  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <SSHKeyManager />
    </div>
  );
}

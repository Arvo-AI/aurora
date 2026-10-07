"use client";

import { AtlassianConnectPage } from "@/components/connectors/AtlassianConnectPage";

export default function ConfluenceConnectPage() {
  return (
    <AtlassianConnectPage
      product={{
        key: "confluence",
        name: "Confluence",
        icon: "/confluence.svg",
        subtitle: "Runbooks & documentation",
        cloudLabel: "Confluence Cloud",
        dcLabel: "Confluence Data Center",
        patUrlPlaceholder: "https://confluence.yourcompany.com",
        storageKey: "isConfluenceConnected",
      }}
      sibling={{
        key: "jira",
        name: "Jira",
        icon: "/jira.svg",
        subtitle: "Issue tracking & incidents",
        connectPath: "/jira/connect",
        enabled: true,
      }}
    />
  );
}

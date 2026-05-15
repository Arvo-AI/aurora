"use client";

import { AtlassianConnectPage } from "@/components/connectors/AtlassianConnectPage";

export default function JiraConnectPage() {
  return (
    <AtlassianConnectPage
      product={{
        key: "jira",
        name: "Jira",
        icon: "/jira.svg",
        subtitle: "Issue tracking & incident management",
        cloudLabel: "Jira Cloud",
        dcLabel: "Jira Data Center",
        patUrlPlaceholder: "https://jira.yourcompany.com",
        storageKey: "isJiraConnected",
      }}
      sibling={{
        key: "confluence",
        name: "Confluence",
        icon: "/confluence.svg",
        subtitle: "Runbooks & documentation",
        connectPath: "/confluence/connect",
        enabled: true,
      }}
    />
  );
}

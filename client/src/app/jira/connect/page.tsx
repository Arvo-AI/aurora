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
        patSteps: [
          "Self-hosted Jira Data Center or Server 8.14+ only. Jira Cloud has no PAT — use Connect with Atlassian above.",
          "Avatar (top right) → Profile → Personal access tokens in the left sidebar (not the main profile page).",
          "Create token, name it Aurora, copy it once, then paste your site URL and token below.",
          "Don't see Personal access tokens? You're on Cloud, on an older Server version, or your admin disabled PATs.",
        ],
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

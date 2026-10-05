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
          "In Jira Data Center, click your profile photo, then Profile.",
          "Open Personal Access Tokens and choose Create token. Name it Aurora.",
          "Copy the token immediately. Jira shows it only once.",
          "Paste your site URL and that token below. Jira Cloud uses the OAuth button above instead.",
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

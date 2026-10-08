export const TEAMS_SETUP_DOCS_URL =
  "https://arvo-ai.github.io/aurora/docs/integrations/connectors#microsoft-teams";

/** Shown in UI — customer org admin checklist (not operator Azure setup). */
export const TEAMS_CUSTOMER_SETUP = {
  headline: "Two steps — both are required",
  summary:
    "Microsoft splits “the bot in Teams” and “Aurora linked to your tenant” into separate actions. Connect in Aurora does not install the app in Teams; installing the app does not link Aurora to your organization.",
  installTitle: "1. Add Aurora in Microsoft Teams (per team)",
  installSteps: [
    "In Teams, open each team where Aurora should work.",
    "Go to Apps and install your organization’s Aurora app (from your tenant catalog or the package your admin provided).",
    "In a channel, @mention the bot (e.g. @Aurora) once so Aurora can discover the channel — or activate channels on the Manage page after step 2.",
  ],
  installWhy:
    "Teams only sends @mentions and delivers bot messages to teams where the app is installed. Aurora cannot do this step for you through OAuth.",
  oauthTitle: "2. Connect in Aurora (once per organization)",
  oauthSteps: [
    "Click Connect below and sign in with a Microsoft work account that can consent for your tenant.",
    "Open Teams → Manage to refresh channels, activate routing, and set the incident card channel.",
  ],
  oauthWhy:
    "OAuth links your Entra tenant to Aurora so we can list channels, read metadata for routing, and associate @mentions with your org. It does not add the bot to any Team.",
} as const;

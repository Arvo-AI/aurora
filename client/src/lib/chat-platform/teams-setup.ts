export const TEAMS_SETUP_DOCS_URL =
  "https://arvo-ai.github.io/aurora/docs/integrations/connectors#microsoft-teams";

/** Shown in Aurora UI — org admins / end users (not platform operators). */
export const TEAMS_CUSTOMER_SETUP = {
  headline: "Three steps — in this order",
  summary:
    "Connect in Aurora first so @mentions can reach your organization. Then add the Aurora Teams app to each team, and @mention the bot once to confirm delivery.",
  orderNote:
    "If you @mention Aurora before Connect, Aurora cannot reply — the tenant is not linked yet.",
  oauthTitle: "1. Connect in Aurora (once per organization)",
  oauthSteps: [
    "Click Connect below and sign in with a Microsoft work account that can consent for your tenant.",
    "After redirect, open Teams → Manage to refresh and activate channels.",
  ],
  oauthWhy:
    "Links your Entra tenant to Aurora. Required before the bot can route @mentions or post incident cards to your org.",
  installTitle: "2. Add Aurora to each Microsoft Team",
  installWhy:
    "Microsoft only sends @mentions and bot messages in teams where the app is installed. Connect in Aurora does not add the app for you.",
  installSteps: [
    "In Teams (desktop or web), open the team where Aurora should work.",
    "Team ··· → Manage team → Apps — or in a channel: + → Add an app.",
    "Search for your organization’s Aurora app (often under Built for your org), then Add to this team → Install.",
  ],
  installNote:
    "Don’t see an Aurora app? Ask your Aurora platform administrator — they publish it to your tenant before users can install it.",
  verifyTitle: "3. Confirm with an @mention",
  verifySteps: [
    "In a channel where the app is installed, send a message that @mentions Aurora (use the app’s display name, e.g. “@Aurora hello”).",
    "On Teams → Manage, click “Check bot connection”. A recent @mention means install and Connect are working.",
  ],
  verifyWhy:
    "Aurora records when the bot receives a channel message. That confirms Teams is delivering traffic to your organization’s Aurora instance.",
} as const;

/** Treat bot activity within this window as a successful verification ping. */
export const TEAMS_BOT_VERIFY_WINDOW_SEC = 15 * 60;

export function teamsOAuthFailureMessage(
  errorCode: string,
  redirectUri?: string,
  msDescription?: string,
): string {
  const desc = msDescription ?? "";
  if (
    desc.includes("/common endpoint") ||
    desc.includes("multi-tenant application") ||
    desc.includes("not configured as a multi-tenant")
  ) {
    return (
      "Your Entra app is single-tenant, but Aurora is using the /common sign-in endpoint (TEAMS_TENANT_ID=common). " +
      "Set TEAMS_TENANT_ID in .env to your Directory (tenant) ID from Microsoft Entra ID → Overview, restart aurora-server, then Connect again. " +
      "Alternatively, change the app registration Supported account types to multitenant."
    );
  }
  const entraAuthPath =
    "Microsoft Entra ID → App registrations (not Enterprise applications) → open the app whose Application (client) ID matches TEAMS_CLIENT_ID in .env → Authentication → Web redirect URIs";
  const redirectHint = redirectUri
    ? ` Under ${entraAuthPath}, this URI must be listed exactly: ${redirectUri}`
    : ` ${entraAuthPath}: use your ngrok host + /teams/callback (not localhost:3000).`;

  const messages: Record<string, string> = {
    access_denied: "Sign-in or consent was cancelled in Microsoft.",
    invalid_request:
      "Microsoft rejected the OAuth request (redirect URI or platform type). Use a single Web platform (not SPA), no trailing slash on the URI, and keep ngrok on port 5080. See the Microsoft line below for the exact AADSTS code." +
      redirectHint,
    no_code_or_state:
      "Microsoft did not return an authorization code. If the redirect URI is already in Entra, confirm it is on the same app registration as TEAMS_CLIENT_ID, keep ngrok running, and finish sign-in in one step." +
      redirectHint,
    invalid_state: "OAuth session expired. Click Connect again and finish Microsoft sign-in without long delays.",
    no_token: "Microsoft sign-in succeeded but Aurora could not read an access token. Try Connect again.",
    unexpected_error: "Something went wrong finishing Teams setup. Check server logs and try Connect again.",
  };
  return messages[errorCode] ?? `Teams setup failed (${errorCode}).${redirectHint}`;
}

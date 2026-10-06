"use client";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { ExternalLink } from "lucide-react";
import Image from "next/image";
import { cn } from "@/lib/utils";

type IdpGuide = {
  id: string;
  name: string;
  logo: string;
  tag?: string;
  consoleUrl: string;
  consoleLabel: string;
  avoid?: string;
  steps: string[];
  fieldMap: { aurora: string; idp: string }[];
  metadataHint: string;
};

const IDP_GUIDES: IdpGuide[] = [
  {
    id: "entra",
    name: "Microsoft Entra ID",
    logo: "/sso-idp/entra.svg",
    consoleUrl: "https://entra.microsoft.com/#view/Microsoft_AAD_IAM/ManagedAppMenuBlade/~/CustomAppApps",
    consoleLabel: "Open Enterprise applications",
    avoid: "App registrations (Redirect URI) is OAuth/OIDC — Aurora uses SAML.",
    steps: [
      "Microsoft Entra admin center → expand Entra ID → Enterprise applications → New application.",
      "Create your own application → name it (e.g. Aurora) → Integrate any other application you don't find in the gallery (Non-gallery) → Create.",
      "Open the new app → Single sign-on → SAML → Edit Basic SAML Configuration → paste Aurora Identifier and Reply URL → Save.",
      "Attributes & Claims: confirm an email claim is present (often user.mail or emailaddress).",
      "Users and groups → assign who may sign in.",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "Identifier (Entity ID)" },
      { aurora: "Reply URL (ACS)", idp: "Reply URL (Assertion Consumer Service URL)" },
      { aurora: "Sign on URL", idp: "Sign on URL (optional; Entra My Apps / IdP-initiated tile)" },
    ],
    metadataHint: "Same SAML blade → SAML Certificates → Federation Metadata XML (download or copy) → Aurora step 3.",
  },
  {
    id: "okta",
    name: "Okta",
    logo: "/sso-idp/okta.svg",
    consoleUrl: "https://login.okta.com/admin/apps/active",
    consoleLabel: "Open Okta Admin → Applications",
    steps: [
      "Okta Admin Console → Applications → Create App Integration → SAML 2.0 → Next.",
      "App name (e.g. Aurora) → Next → Configure SAML: paste Aurora Audience URI and Single sign-on URL → Next → Finish.",
      "Sign On tab → Edit → Name ID format: EmailAddress (recommended).",
      "Assignments → assign users or groups.",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "Audience URI (SP Entity ID)" },
      { aurora: "Reply URL (ACS)", idp: "Single sign-on URL (Okta’s label for the SP ACS URL)" },
    ],
    metadataHint: "Sign On tab → Metadata URL, or Identity Provider metadata download → paste XML into Aurora step 3.",
  },
  {
    id: "google",
    name: "Google Workspace",
    logo: "/sso-idp/google.svg",
    consoleUrl: "https://admin.google.com/ac/apps/unified",
    consoleLabel: "Open Google Admin → Apps",
    steps: [
      "Google Admin console → Apps → Web and mobile apps → Add app → Add custom SAML app.",
      "App name → Continue → Google IdP details → Continue (download metadata here if you want it for Aurora step 3).",
      "Service provider details → Entity ID = Aurora Identifier; ACS URL = Aurora Reply URL. Start URL optional (Aurora Sign on URL). Name ID format: EMAIL.",
      "Attribute mapping → Continue → Turn ON for your organizational unit → User access → assign groups.",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "Service provider details → Entity ID" },
      { aurora: "Reply URL (ACS)", idp: "Service provider details → ACS URL" },
      { aurora: "Sign on URL", idp: "Service provider details → Start URL (optional)" },
    ],
    metadataHint: "Google IdP details page → Download metadata, or copy SSO URL + Entity ID + certificate → Aurora step 3.",
  },
  {
    id: "keycloak",
    name: "Keycloak",
    logo: "/sso-idp/keycloak.svg",
    tag: "Self-hosted",
    consoleUrl: "https://www.keycloak.org/docs/latest/server_admin/#_saml_clients",
    consoleLabel: "Keycloak SAML client docs",
    steps: [
      "Keycloak Admin Console → select realm → Clients → Create client.",
      "Client type SAML → Client ID = Aurora Identifier (Entity ID) → Next → Save.",
      "Client settings → Valid redirect URIs = Aurora Reply URL (ACS). Save.",
      "Use the realm’s default SAML mappers so email is in the assertion (or add an email mapper on the client).",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "Client ID" },
      { aurora: "Reply URL (ACS)", idp: "Valid redirect URIs (SAML POST ACS)" },
    ],
    metadataHint: "Realm → Realm settings → General → Endpoints → SAML 2.0 Identity Provider Metadata (URL or download) → Aurora step 3.",
  },
];

function IdpLogo({ guide }: Readonly<{ guide: IdpGuide }>) {
  return (
    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border bg-background p-1">
      <Image
        src={guide.logo}
        alt=""
        width={20}
        height={20}
        className={cn(
          "object-contain",
          guide.id === "google" ? "h-6 w-6 dark:invert" : "h-5 w-5 dark:invert-[.85]",
        )}
      />
    </span>
  );
}

function GuideBody({ guide }: Readonly<{ guide: IdpGuide }>) {
  return (
    <div className="space-y-4 text-sm">
      {guide.avoid && (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs">
          {guide.avoid}
        </p>
      )}
      <ol className="list-decimal space-y-1.5 pl-4 text-muted-foreground">
        {guide.steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
      <div>
        <p className="text-xs font-medium text-foreground mb-2">Map Aurora → your IdP</p>
        <div className="rounded-md border divide-y text-xs">
          {guide.fieldMap.map((row) => (
            <div key={row.aurora} className="grid grid-cols-1 sm:grid-cols-2 gap-1 px-3 py-2">
              <span className="font-mono text-muted-foreground">{row.aurora}</span>
              <span>{row.idp}</span>
            </div>
          ))}
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        <span className="text-foreground font-medium">Bring back to Aurora:</span> {guide.metadataHint}
      </p>
      <a
        href={guide.consoleUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 text-xs text-primary hover:underline"
      >
        {guide.consoleLabel}
        <ExternalLink className="h-3 w-3" />
      </a>
    </div>
  );
}

export function SsoIdpSetupGuides() {
  return (
    <div className="rounded-lg border bg-muted/20 p-4">
      <div className="mb-3">
        <p className="text-sm font-medium">Setup guides by provider</p>
        <p className="text-xs text-muted-foreground mt-0.5">
          Create a <span className="text-foreground">SAML</span> enterprise app — not an OAuth app with a single redirect URI.
        </p>
      </div>
      <Accordion type="single" collapsible className="w-full">
        {IDP_GUIDES.map((guide) => (
          <AccordionItem key={guide.id} value={guide.id} className="border-border/60">
            <AccordionTrigger className="py-3 text-sm hover:no-underline">
              <span className="flex items-center gap-2.5">
                <IdpLogo guide={guide} />
                {guide.name}
                {guide.tag && (
                  <Badge variant="secondary" className="text-[10px] px-1.5 py-0 font-normal">
                    {guide.tag}
                  </Badge>
                )}
              </span>
            </AccordionTrigger>
            <AccordionContent>
              <GuideBody guide={guide} />
            </AccordionContent>
          </AccordionItem>
        ))}
      </Accordion>
    </div>
  );
}

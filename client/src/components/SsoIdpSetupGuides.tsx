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
      "Entra ID → Applications → Enterprise applications → New application.",
      "Create your own application → name it (e.g. Aurora) → Integrate any other application you don't find in the gallery (Non-gallery) → Create.",
      "Single sign-on → SAML → Edit Basic SAML Configuration.",
      "Assign users or groups under Users and groups.",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "Identifier (Entity ID)" },
      { aurora: "Reply URL (ACS)", idp: "Reply URL (Assertion Consumer Service URL)" },
      { aurora: "Sign on URL", idp: "Sign on URL (optional; My Apps tile)" },
      { aurora: "Metadata URL", idp: "Or upload metadata from this URL instead of typing the two fields above" },
    ],
    metadataHint: "SAML Certificates → Federation Metadata XML — paste into Aurora step 3.",
  },
  {
    id: "okta",
    name: "Okta",
    logo: "/sso-idp/okta.svg",
    consoleUrl: "https://login.okta.com/admin/apps/active",
    consoleLabel: "Open Okta Admin → Applications",
    steps: [
      "Applications → Create App Integration → SAML 2.0 → Next.",
      "App name (e.g. Aurora) → Next → paste Aurora values on the SAML settings screen → Finish.",
      "Assignments → assign users or groups.",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "Audience URI (SP Entity ID)" },
      { aurora: "Reply URL (ACS)", idp: "Single sign-on URL" },
      { aurora: "Metadata URL", idp: "Optional: some tenants import SP metadata from URL" },
    ],
    metadataHint: "Sign On tab → View SAML setup instructions → Identity Provider metadata (or copy Issuer, SSO URL, cert).",
  },
  {
    id: "google",
    name: "Google Workspace",
    logo: "/sso-idp/google.svg",
    consoleUrl: "https://admin.google.com/ac/apps/unified",
    consoleLabel: "Open Google Admin → Apps",
    steps: [
      "Apps → Web and mobile apps → Add app → Add custom SAML app.",
      "Download IdP metadata or copy SSO URL, Entity ID, and certificate on the Google IdP details page.",
      "On the Service provider details page, paste Aurora's Entity ID and ACS URL.",
      "Turn the app ON for your organizational unit and assign users.",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "ACS URL / Entity ID fields (Google labels vary by step)" },
      { aurora: "Reply URL (ACS)", idp: "ACS URL" },
    ],
    metadataHint: "App details → Download metadata — paste XML into Aurora step 3.",
  },
  {
    id: "keycloak",
    name: "Keycloak",
    logo: "/sso-idp/keycloak.svg",
    tag: "Self-hosted",
    consoleUrl: "https://www.keycloak.org/guides",
    consoleLabel: "Keycloak admin console",
    steps: [
      "Select realm → Clients → Create client → Client type SAML → Next.",
      "Client ID: Aurora Entity ID. Root URL: your Aurora backend base URL (optional).",
      "Settings → Master SAML Processing URL / ACS: Aurora Reply URL (ACS).",
      "Keys tab → export the signing certificate for Aurora step 3.",
    ],
    fieldMap: [
      { aurora: "Identifier (Entity ID)", idp: "Client ID" },
      { aurora: "Reply URL (ACS)", idp: "Valid redirect URIs / ACS URL" },
    ],
    metadataHint: "Realm settings → SAML 2.0 Identity Provider Metadata, or copy from the client Keys tab.",
  },
];

function IdpLogo({ guide }: Readonly<{ guide: IdpGuide }>) {
  // White chip keeps mono marks readable in dark UI; Keycloak’s mark is wider than it is tall.
  const keycloak = guide.id === "keycloak";
  return (
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border/60 bg-white p-1">
      <Image
        src={guide.logo}
        alt=""
        width={24}
        height={24}
        className={cn("object-contain", keycloak ? "h-3.5 w-auto max-w-[1.65rem]" : "h-5 w-5")}
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

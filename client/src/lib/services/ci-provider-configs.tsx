"use client";

import { ExternalLink } from "lucide-react";
import {
  type CIProviderConfig,
  jenkinsService,
  cloudbeesService,
} from "@/lib/services/ci-provider";

export const jenkinsConfig: CIProviderConfig = {
  slug: "jenkins",
  displayName: "Jenkins",
  description: "Read-only access to jobs, builds, pipelines, and agents",
  logoPath: "/jenkins.svg",
  logoAlt: "Jenkins",
  accentColor: "orange-600",
  accentTextColor: "orange",
  cacheKey: "jenkins_connection_status",
  localStorageConnectedKey: "isJenkinsConnected",
  urlPlaceholder: "https://jenkins.example.com",
  urlHelpText: "Full URL to your Jenkins instance, without a trailing slash",
  usernamePlaceholder: "your-jenkins-username",
  setupStepTitle: "Generate an API Token in Jenkins",
  setupStepNavPath:
    "Configure → API Token → Add new Token",
  setupStepInstructions: [
    "Sign in to your Jenkins instance as the user you want to connect",
    <>
      Click your username in the top-right corner, then go to:
      <code className="block px-3 py-2 bg-muted rounded text-xs mt-1.5 font-mono">
        Configure &rarr; API Token &rarr; Add new Token
      </code>
    </>,
    <>Give the token a name (e.g. <strong>Aurora</strong>) and click <strong>Generate</strong></>,
    <span className="text-orange-700 dark:text-orange-400 font-medium">
      Copy the token immediately &mdash; it won&apos;t be shown again
    </span>,
  ],
  docsUrl:
    "https://www.jenkins.io/doc/book/system-administration/authenticating-scripted-clients/",
  docsLabel: "Jenkins API Authentication Docs",
  service: jenkinsService,
};

export const cloudbeesConfig: CIProviderConfig = {
  slug: "cloudbees",
  displayName: "CloudBees CI",
  description: "Read-only access to jobs, builds, pipelines, and agents",
  logoPath: "/cloudbees.svg",
  logoAlt: "CloudBees",
  accentColor: "violet-600",
  accentTextColor: "violet",
  cacheKey: "cloudbees_connection_status",
  localStorageConnectedKey: "isCloudBeesConnected",
  urlPlaceholder: "https://cloudbees.example.com",
  urlHelpText:
    "Full URL to your CloudBees CI instance or Operations Center (e.g. https://cloudbees.example.com/cjoc)",
  usernamePlaceholder: "your-cloudbees-username",
  setupStepTitle: "Generate an API Token in CloudBees CI",
  setupStepNavPath:
    "Security → API Token → Add new Token",
  setupStepInstructions: [
    "Sign in to your CloudBees CI instance as the user you want to connect",
    <>
      Click your username in the top-right corner, then go to:
      <code className="block px-3 py-2 bg-muted rounded text-xs mt-1.5 font-mono">
        Security &rarr; API Token &rarr; Add new Token
      </code>
    </>,
    <>Give the token a name (e.g. <strong>Aurora</strong>) and click <strong>Generate</strong></>,
    <span className="text-violet-700 dark:text-violet-400 font-medium">
      Copy the token immediately &mdash; it won&apos;t be shown again
    </span>,
  ],
  setupStepNote: (
    <div className="mt-3 p-3 rounded-lg bg-muted/50 text-xs space-y-2">
      <p className="text-muted-foreground">
        <strong className="text-foreground">Operations Center:</strong> If using
        managed controllers, generate tokens at the Operations Center level to
        avoid sync issues.
      </p>
      <a
        href="https://docs.cloudbees.com/docs/cloudbees-ci-api/latest/api-authentication"
        target="_blank"
        rel="noopener noreferrer"
        className="text-xs text-muted-foreground hover:text-foreground hover:underline flex items-center gap-1"
      >
        CloudBees CI API Authentication Docs
        <ExternalLink className="w-3 h-3" />
      </a>
    </div>
  ),
  docsUrl:
    "https://docs.cloudbees.com/docs/cloudbees-ci-api/latest/api-authentication",
  docsLabel: "CloudBees CI API Authentication Docs",
  service: cloudbeesService,
};

export const ciProviderConfigs: Record<string, CIProviderConfig> = {
  jenkins: jenkinsConfig,
  cloudbees: cloudbeesConfig,
};

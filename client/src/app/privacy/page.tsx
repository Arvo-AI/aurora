"use client";

import Image from "next/image";
import Link from "next/link";
import { useState } from "react";

export default function PrivacyPolicy() {
  const [year, setYear] = useState(new Date().getFullYear());

  return (
    <div className="min-h-screen bg-background flex flex-col justify-between">
      <main className="flex-grow py-12 px-4 sm:px-6 lg:px-8">
        <div className="max-w-4xl mx-auto">
          <div className="mb-8 flex items-center">
            <Link href="/" className="inline-block">
              <Image
                src="/arvologo.png"
                alt="Aurora by Arvo"
                width={80}
                height={80}
                sizes="80px"
              />
            </Link>
            <h1 className="ml-4 text-3xl font-bold text-foreground">Privacy Notice</h1>
          </div>

          <div className="bg-card p-8 rounded-lg shadow-md">
            <div className="prose prose-invert max-w-none">
              <p className="text-muted-foreground mb-4">Last Updated: March 8, 2026</p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Who We Are</h2>
              <p className="mb-4 text-foreground">
                Arvo AI Ltd. is a Canadian company that develops Aurora, an open-source AI-powered root cause analysis platform for Site Reliability Engineers and DevOps teams. For any privacy-related inquiries, contact us at{" "}
                <a href="mailto:noah@arvoai.ca" className="text-blue-500 hover:text-blue-400 hover:underline">noah@arvoai.ca</a>.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">What Data We Collect</h2>
              <p className="mb-4 text-foreground">
                When you use Aurora, we process the following categories of data:
              </p>
              <ul className="list-disc pl-6 mb-4 text-foreground">
                <li>Your name, email address, and organizational role (for account authentication)</li>
                <li>Your investigation queries and results</li>
                <li>Infrastructure telemetry data (logs, metrics, traces) queried on-demand from your cloud providers</li>
                <li>Your cloud provider credentials and API keys (collected for connector setup)</li>
              </ul>

              <h2 className="text-xl font-semibold mb-4 text-foreground">How We Collect It</h2>
              <p className="mb-4 text-foreground">
                Account data is provided by you or your organization's administrator during setup. Infrastructure data is queried from your own cloud providers only when you initiate an investigation. Credentials are provided during connector configuration.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Purpose of Processing</h2>
              <p className="mb-4 text-foreground">
                To provide AI-assisted root cause analysis of infrastructure incidents, enabling your team to investigate and resolve production incidents.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Legal Basis</h2>
              <p className="mb-4 text-foreground">
                Contract (Article 6(1)(b) GDPR). Processing is necessary to deliver the Aurora service as agreed in our service agreement. For self-hosted deployments, your organization is the data controller and Arvo AI acts as data processor.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Data Storage and Security</h2>
              <p className="mb-4 text-foreground">
                Aurora is deployed on your organization's own infrastructure. All data remains under your control. Credentials are stored in HashiCorp Vault with encryption, separate from the application database. No telemetry or analytics are collected by Arvo AI. The codebase is open-source under the{" "}
                <a href="https://www.apache.org/licenses/LICENSE-2.0" target="_blank" rel="noopener noreferrer" className="text-blue-500 hover:text-blue-400 hover:underline">Apache License 2.0</a>{" "}
                and available for security audit.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Data Sharing</h2>
              <p className="mb-4 text-foreground">
                No data is shared with Arvo AI. When you use an external LLM provider (OpenAI, Anthropic, OpenRouter) for AI analysis, investigation queries are sent to that provider. You may configure a self-hosted LLM to eliminate this transfer entirely.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Data Retention</h2>
              <p className="mb-4 text-foreground">
                Your organization controls data retention on your self-hosted deployment. Account data is retained for the duration of the service agreement. Investigation data retention is configurable by your organization.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Your Rights</h2>
              <p className="mb-4 text-foreground">
                Under GDPR, you have the right to:
              </p>
              <ul className="list-disc pl-6 mb-4 text-foreground">
                <li>Access your personal data</li>
                <li>Rectify inaccurate data</li>
                <li>Erase your data ("right to be forgotten")</li>
                <li>Restrict processing</li>
                <li>Object to processing</li>
                <li>Data portability</li>
              </ul>
              <p className="mb-4 text-foreground">
                To exercise any of these rights, contact your organization's administrator or Arvo AI at{" "}
                <a href="mailto:noah@arvoai.ca" className="text-blue-500 hover:text-blue-400 hover:underline">noah@arvoai.ca</a>.
                You also have the right to lodge a complaint with your local supervisory authority.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">Updates to This Notice</h2>
              <p className="mb-4 text-foreground">
                This notice may be updated periodically. Changes will be communicated through our documentation and release notes.
              </p>
            </div>
          </div>
        </div>
      </main>

      <footer className="py-6 bg-card border-t border-border">
        <div className="container mx-auto px-4">
          <div className="flex flex-col md:flex-row justify-between items-center">
            <div className="mb-4 md:mb-0">
              <p className="text-gray-600 text-sm">
                &copy; {year} Arvo A.I. Ltd. All rights reserved.
              </p>
            </div>

            <div className="flex space-x-6">
              <Link href="/" className="text-gray-600 hover:text-gray-900 text-sm">
                Home
              </Link>
              <Link href="/terms" className="text-gray-600 hover:text-gray-900 text-sm">
                Terms of Service
              </Link>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}

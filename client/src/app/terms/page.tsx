"use client";

import Image from "next/image";
import Link from "next/link";
import { useState } from "react";

export default function TermsOfService() {
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
            <h1 className="ml-4 text-3xl font-bold text-foreground">Terms of Service</h1>
          </div>

          <div className="bg-card p-8 rounded-lg shadow-md">
            <div className="prose prose-invert max-w-none">
              <p className="text-muted-foreground mb-4">Last Updated: {new Date().toLocaleDateString()}</p>
              
              <h2 className="text-xl font-semibold mb-4 text-foreground">1. Agreement to Terms</h2>
              <p className="mb-4 text-foreground">
                By accessing or using Aurora, you agree to be bound by these Terms of Service and all applicable laws and regulations. If you do not agree with any of these terms, you are prohibited from using or accessing Aurora.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">2. Open Source Software</h2>
              <p className="mb-4 text-foreground">
                Aurora is open source software licensed under the <a href="https://www.apache.org/licenses/LICENSE-2.0" target="_blank" rel="noopener noreferrer" className="text-blue-500 hover:text-blue-400 hover:underline">Apache License 2.0</a>. You may use, modify, and distribute Aurora in accordance with the terms of the Apache License.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">3. Use of Service</h2>
              <p className="mb-4 text-foreground">
                Aurora provides a natural language interface for managing and optimizing cloud infrastructure. You agree to use Aurora only for lawful purposes and in accordance with these Terms.
              </p>
              <p className="mb-4 text-foreground">
                When you self-host Aurora, you are responsible for maintaining the security of your deployment, including protecting credentials and access to your infrastructure. You agree to follow security best practices when deploying and operating Aurora.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">4. Cloud Service Access</h2>
              <p className="mb-4 text-foreground">
                Aurora requires access to your cloud service providers (such as AWS, GCP, Azure, OVH, Scaleway, etc.) to function properly. You are responsible for configuring appropriate permissions and access controls for Aurora.
              </p>
              <p className="mb-4 text-foreground">
                <strong>Important:</strong> Aurora will make changes to your cloud infrastructure based on your instructions. You are solely responsible for reviewing and authorizing any changes before they are executed. We strongly recommend testing in non-production environments first.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">5. Intellectual Property and Trademarks</h2>
              <p className="mb-4 text-foreground">
                The Aurora software is licensed under Apache License 2.0, which grants you broad rights to use, modify, and distribute the software. However, the "Aurora" name and Arvo AI branding are trademarks of Arvo A.I. Ltd.
              </p>
              <p className="mb-4 text-foreground">
                The Apache License does not grant permission to use trade names, trademarks, or service marks of Arvo A.I. Ltd., except as required for reasonable and customary use in describing the origin of the software.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">6. Disclaimer of Warranty</h2>
              <p className="mb-4 text-foreground">
                Aurora is provided "AS IS" WITHOUT WARRANTY OF ANY KIND, either express or implied, including but not limited to warranties of merchantability, fitness for a particular purpose, and non-infringement. This is consistent with the Apache License 2.0.
              </p>
              <p className="mb-4 text-foreground">
                You are solely responsible for determining the appropriateness of using Aurora and assume all risks associated with infrastructure changes made through the software.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">7. Limitation of Liability</h2>
              <p className="mb-4 text-foreground">
                To the maximum extent permitted by law, in no event shall Arvo A.I. Ltd. or contributors be liable for any direct, indirect, incidental, special, consequential, or punitive damages resulting from your use of Aurora, including but not limited to:
              </p>
              <ul className="list-disc pl-6 mb-4 text-foreground">
                <li>Loss of revenue, profits, or data</li>
                <li>Infrastructure outages or disruptions</li>
                <li>Unauthorized access or security breaches</li>
                <li>Costs of procurement of substitute services</li>
              </ul>
              <p className="mb-4 text-foreground">
                This limitation applies even if advised of the possibility of such damages.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">8. Your Responsibilities</h2>
              <p className="mb-4 text-foreground">
                When using Aurora, you agree to:
              </p>
              <ul className="list-disc pl-6 mb-4 text-foreground">
                <li>Review all infrastructure changes before execution</li>
                <li>Maintain appropriate backups of your infrastructure</li>
                <li>Follow security best practices for credential management</li>
                <li>Comply with your cloud provider's terms of service</li>
                <li>Test changes in non-production environments when possible</li>
              </ul>

              <h2 className="text-xl font-semibold mb-4 text-foreground">9. Changes to Terms</h2>
              <p className="mb-4 text-foreground">
                We may update these Terms from time to time. Significant changes will be posted on our repository and website. Your continued use of Aurora after changes constitutes acceptance of the updated terms.
              </p>

              <h2 className="text-xl font-semibold mb-4 text-foreground">10. Governing Law</h2>
              <p className="mb-4 text-foreground">
                These Terms shall be governed by and construed in accordance with the laws of the Province of Quebec, Canada, without regard to its conflict of law provisions.
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
              <Link href="/privacy" className="text-gray-600 hover:text-gray-900 text-sm">
                Privacy Policy
              </Link>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
} 
"use client";

import CIProviderAuthPage from "@/components/ci-provider-auth-page";
import { jenkinsConfig } from "@/lib/services/ci-provider-configs";

export default function JenkinsAuthPage() {
  return <CIProviderAuthPage config={jenkinsConfig} />;
}

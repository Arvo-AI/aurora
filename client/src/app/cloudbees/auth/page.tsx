"use client";

import CIProviderAuthPage from "@/components/ci-provider-auth-page";
import { cloudbeesConfig } from "@/lib/services/ci-provider-configs";

export default function CloudBeesAuthPage() {
  return <CIProviderAuthPage config={cloudbeesConfig} />;
}

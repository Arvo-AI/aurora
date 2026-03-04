"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

export default function ConfluenceCallbackRedirect() {
  const router = useRouter();
  const searchParams = useSearchParams();
  useEffect(() => {
    const params = searchParams.toString();
    router.replace(`/atlassian/callback${params ? `?${params}` : ""}`);
  }, [router, searchParams]);
  return null;
}

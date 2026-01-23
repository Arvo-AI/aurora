"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";
import Image from "next/image";

/**
 * OVH Callback Page
 * 
 * This page is now a fallback/legacy handler. The primary OAuth flow uses
 * backend-first redirects (backend receives callback and redirects to /chat).
 * 
 * This page handles:
 * 1. Legacy frontend callbacks (if any)
 * 2. Direct navigation (shows error)
 * 3. Redirect to onboarding on error
 */
function OvhCallbackContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const errorParam = searchParams.get('error');
    const errorDescription = searchParams.get('error_description');

    // If there's an error, display it and redirect to onboarding
    if (errorParam) {
      setError(errorDescription || errorParam);
      return;
    }

    // If someone lands here without proper params, redirect to onboarding
    const code = searchParams.get('code');
    const state = searchParams.get('state');
    
    if (!code && !state) {
      // Direct navigation - redirect to onboarding
      router.replace('/ovh/onboarding');
      return;
    }

    // If we have code/state, the backend should have handled this
    // This is likely a misconfigured redirect URI - show error
    setError('OAuth callback reached frontend instead of backend. Please check redirect URI configuration.');
  }, [searchParams, router]);

  if (error) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center px-4">
        <div className="max-w-md w-full text-center">
          <Image
            src="/ovh.svg"
            alt="OVH Cloud"
            width={64}
            height={64}
            className="mx-auto mb-4"
          />
          <h1 className="text-2xl font-bold text-foreground mb-2">Connection Failed</h1>
          <p className="text-destructive mb-6">{error}</p>
          <button
            onClick={() => router.push('/ovh/onboarding')}
            className="px-6 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors"
          >
            Try Again
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-4">
      <div className="max-w-md w-full text-center">
        <Image
          src="/ovh.svg"
          alt="OVH Cloud"
          width={64}
          height={64}
          className="mx-auto mb-4"
        />
        <h1 className="text-2xl font-bold text-foreground mb-2">Connecting to OVH Cloud</h1>
        <div className="flex items-center justify-center gap-2 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
          <span>Completing authentication...</span>
        </div>
      </div>
    </div>
  );
}

export default function OvhCallbackPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen bg-background flex items-center justify-center px-4">
        <div className="max-w-md w-full text-center">
          <Loader2 className="w-8 h-8 animate-spin mx-auto text-muted-foreground" />
        </div>
      </div>
    }>
      <OvhCallbackContent />
    </Suspense>
  );
}

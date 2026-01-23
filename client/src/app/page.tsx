"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useUser } from '@/hooks/useAuthHooks';

export default function HomePage() {
  const router = useRouter();
  const { user, isLoaded } = useUser();
  const isHandlingRedirect = useRef(false);

  useEffect(() => {
    const handleRedirect = async () => {
      if (!isLoaded || isHandlingRedirect.current) return;
      
      isHandlingRedirect.current = true;

      if (user) {
        // Authenticated user - redirect to incidents
        router.replace("/incidents");
      } else {
        // No authenticated user - redirect to sign-in
        router.replace("/sign-in");
      }
    };

    handleRedirect();
  }, [isLoaded, user, router]);

  // Show loading state while redirecting
  return (
    <div className="flex items-center justify-center min-h-screen">
      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary" />
    </div>
  );
}

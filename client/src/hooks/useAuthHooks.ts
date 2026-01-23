"use client";

import { useSession } from "next-auth/react";

/**
 * Custom hook to replace Auth.js's useUser hook
 * Returns Auth.js session data in a Auth.js-compatible format
 */
export function useUser() {
  const { data: session, status } = useSession();
  
  const isLoaded = status !== "loading";
  const isSignedIn = !!session?.user;
  
  // Map Auth.js user to Auth.js-like user object
  const user = session?.user ? {
    id: session.user.id!,
    email: session.user.email || "",
    emailAddresses: session.user.email ? [{ emailAddress: session.user.email }] : [],
    fullName: session.user.name || null,
    firstName: session.user.name?.split(' ')[0] || null,
    imageUrl: session.user.image || null,
  } : null;

  return {
    isLoaded,
    isSignedIn,
    user,
  };
}

/**
 * Custom hook to replace Auth.js's useAuth hook
 */
export function useAuth() {
  const { data: session, status } = useSession();
  
  return {
    isLoaded: status !== "loading",
    isSignedIn: !!session?.user,
    userId: session?.user?.id || null,
  };
}

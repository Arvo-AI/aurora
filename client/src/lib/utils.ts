import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Extract user-friendly error message from various error formats.
 * Used across connector auth pages.
 */
export function getUserFriendlyError(err: any): string {
  if (!err) return "An unexpected error occurred. Please try again.";

  let errorText = "";

  if (typeof err.message === "string") {
    try {
      const parsed = JSON.parse(err.message);
      errorText = parsed.error || err.message;
    } catch {
      errorText = err.message;
    }
  } else if (err.error) {
    errorText = typeof err.error === "string" ? err.error : JSON.stringify(err.error);
  } else {
    errorText = err.message || err.toString() || "An unexpected error occurred";
  }

  errorText = errorText.replace(/^\d{3}\s+(Client|Server)\s+Error:\s*/i, "");

  if (errorText.length > 0) {
    errorText = errorText.charAt(0).toUpperCase() + errorText.slice(1);
  }

  return errorText || "An unexpected error occurred. Please try again.";
}
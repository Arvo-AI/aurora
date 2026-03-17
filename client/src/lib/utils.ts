import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Extract user-friendly error message from various error formats.
 * Used across connector auth pages.
 */
/**
 * Copy text to clipboard with fallback for non-HTTPS contexts (e.g. VM deployments over HTTP).
 * navigator.clipboard requires a secure context; this falls back to execCommand('copy').
 */
export async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
  } else {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
  }
}

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
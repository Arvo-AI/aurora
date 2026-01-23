"use client";

import { useState, useEffect, useRef } from "react";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { AlertTriangle, ExternalLink, Globe, Copy, Check } from "lucide-react";
import { detectWebView, type WebViewDetectionResult } from "@/lib/webview-detection";

/**
 * Attempts to open the current URL in the device's default browser.
 * Tries multiple strategies depending on the platform.
 * 
 * @returns true if a redirect was attempted
 */
function tryOpenInDefaultBrowser(): boolean {
  if (typeof window === "undefined") return false;
  
  const currentUrl = window.location.href;
  const userAgent = navigator.userAgent;
  
  // Strategy 1: Android Intent (most reliable for Android)
  if (/Android/i.test(userAgent)) {
    try {
      // Build intent URL - this tells Android to open in the default browser
      const intentUrl = `intent://${window.location.host}${window.location.pathname}${window.location.search}${window.location.hash}#Intent;scheme=https;action=android.intent.action.VIEW;end`;
      window.location.href = intentUrl;
      return true;
    } catch {
      // Try alternative: Chrome-specific intent
      try {
        const chromeIntent = `intent://${window.location.host}${window.location.pathname}${window.location.search}#Intent;scheme=https;package=com.android.chrome;end`;
        window.location.href = chromeIntent;
        return true;
      } catch {
        // Continue to other strategies
      }
    }
  }
  
  // Strategy 2: iOS Safari scheme
  if (/iPhone|iPad|iPod/i.test(userAgent)) {
    // Try x-safari scheme (works in some WebViews)
    try {
      // Some apps support this scheme to open in Safari
      window.location.href = `x-safari-${currentUrl}`;
      return true;
    } catch {
      // Continue to other strategies
    }
  }
  
  // Strategy 3: Try window.open with _system target (Cordova/PhoneGap style)
  try {
    const newWindow = window.open(currentUrl, "_system");
    if (newWindow) return true;
  } catch {
    // Continue
  }
  
  // Strategy 4: Try window.open with _blank and specific features
  try {
    const newWindow = window.open(currentUrl, "_blank", "location=yes");
    if (newWindow) return true;
  } catch {
    // Continue
  }
  
  return false;
}

/**
 * WebViewWarning Component
 * 
 * When the app is opened in a WebView/in-app browser:
 * 1. First, automatically tries to open in the default browser
 * 2. If that fails, displays a BLOCKING dialog that cannot be dismissed
 * 
 * Google OAuth doesn't work in WebViews, so users MUST open in a real browser.
 */
export function WebViewWarning() {
  const [isOpen, setIsOpen] = useState(false);
  const [detection, setDetection] = useState<WebViewDetectionResult | null>(null);
  const [copied, setCopied] = useState(false);
  const [redirectAttempted, setRedirectAttempted] = useState(false);
  const hasTriedRedirect = useRef(false);

  useEffect(() => {
    // Only run on client
    if (typeof window === "undefined") return;
    
    // Only try once
    if (hasTriedRedirect.current) return;

    const result = detectWebView();
    setDetection(result);

    if (result.isWebView) {
      hasTriedRedirect.current = true;
      
      // Try to automatically open in default browser
      const redirected = tryOpenInDefaultBrowser();
      setRedirectAttempted(true);
      
      // Show the dialog after a short delay
      // This gives the browser redirect time to work, but shows fallback if it doesn't
      setTimeout(() => {
        setIsOpen(true);
      }, redirected ? 1500 : 0);
    }
  }, []);

  const handleOpenInBrowser = () => {
    tryOpenInDefaultBrowser();
  };

  const handleCopyUrl = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for older browsers
      const textArea = document.createElement("textarea");
      textArea.value = window.location.href;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand("copy");
      document.body.removeChild(textArea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  if (!detection?.isWebView) return null;

  return (
    <>
      {/* Solid black background overlay */}
      {isOpen && <div className="fixed inset-0 z-40 bg-black" />}
      
      <AlertDialog open={isOpen} onOpenChange={() => {/* Prevent closing */}}>
        <AlertDialogContent 
          className="max-w-md"
          onEscapeKeyDown={(e: KeyboardEvent) => e.preventDefault()}
          onPointerDownOutside={(e: Event) => e.preventDefault()}
          onInteractOutside={(e: Event) => e.preventDefault()}
        >
        <AlertDialogHeader>
          <div className="flex items-center gap-3 mb-2">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-red-500/10">
              <AlertTriangle className="h-6 w-6 text-red-500" />
            </div>
            <AlertDialogTitle className="text-xl">
              Unsupported WebView
            </AlertDialogTitle>
          </div>
          <AlertDialogDescription asChild>
            <div className="text-left space-y-3">
              {redirectAttempted && (
                <p className="text-sm text-amber-500 font-medium">️ We tried to open your browser automatically but it may not have worked.
                </p>
              )}
              <p>
                You&apos;re viewing Aurora in{" "}
                <span className="font-semibold text-foreground">
                  {detection.detectedApp || "an in-app browser"}
                </span>.
              </p>
              <p>
                Google Sign-In is{" "}
                <span className="font-semibold text-red-500">blocked</span> in 
                in-app browsers due to Google&apos;s security policies.
              </p>
              <div className="rounded-lg bg-muted p-3 mt-4">
                <p className="text-sm font-medium text-foreground mb-2">
                  Please open Aurora in one of these browsers:
                </p>
                <p className="text-sm flex items-center gap-1.5 flex-wrap">
                  <Globe className="h-4 w-4 text-muted-foreground" />
                  <span>Chrome, Safari, Firefox, or Edge</span>
                </p>
              </div>
              
              {/* URL display for manual copying */}
              <div className="rounded-lg border bg-background p-3 mt-3">
                <p className="text-xs text-muted-foreground mb-2">
                  Copy this URL and paste it in your browser:
                </p>
                <code className="text-xs break-all text-foreground">
                  {typeof window !== "undefined" ? window.location.href : ""}
                </code>
              </div>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="flex-col gap-2 sm:flex-row">
          <Button
            onClick={handleOpenInBrowser}
            className="flex items-center gap-2 w-full sm:w-auto"
          >
            <ExternalLink className="h-4 w-4" />
            Open in Browser
          </Button>
          <Button
            onClick={handleCopyUrl}
            variant="outline"
            className="flex items-center gap-2 w-full sm:w-auto"
          >
            {copied ? (
              <>
                <Check className="h-4 w-4 text-green-500" />
                Copied!
              </>
            ) : (
              <>
                <Copy className="h-4 w-4" />
                Copy URL
              </>
            )}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
      </AlertDialog>
    </>
  );
}


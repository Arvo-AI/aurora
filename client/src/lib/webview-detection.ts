/**
 * WebView Detection Utility
 * 
 * Detects if the app is running inside a WebView (in-app browser).
 * Google OAuth blocks authentication from WebViews, so we need to warn users.
 * 
 * Common WebView indicators in User-Agent:
 * - "wv" or "WebView" - Generic WebView indicator
 * - "FBAN" or "FBAV" - Facebook app
 * - "Instagram" - Instagram app
 * - "Twitter" - Twitter/X app
 * - "LinkedIn" - LinkedIn app
 * - "Slack" - Slack app
 * - "Discord" - Discord app
 * - "Snapchat" - Snapchat app
 * - "Pinterest" - Pinterest app
 * - "TikTok" - TikTok app
 * - "Line" - Line messenger
 * - "WeChat" or "MicroMessenger" - WeChat
 * - "Telegram" - Telegram app
 * - "Notion" - Notion app
 */

export interface WebViewDetectionResult {
  isWebView: boolean;
  detectedApp: string | null;
  userAgent: string;
}

/**
 * List of known WebView/in-app browser patterns
 * Each entry has a pattern to match and a friendly name
 */
const WEBVIEW_PATTERNS: Array<{ pattern: RegExp; name: string }> = [
  // Generic WebView indicators
  { pattern: /\bwv\b/i, name: "WebView" },
  { pattern: /WebView/i, name: "WebView" },
  
  // Social media apps
  { pattern: /FBAN|FBAV/i, name: "Facebook" },
  { pattern: /\bInstagram\b/i, name: "Instagram" },
  { pattern: /\bTwitter\b/i, name: "Twitter/X" },
  { pattern: /\bLinkedIn\b/i, name: "LinkedIn" },
  { pattern: /\bSnapchat\b/i, name: "Snapchat" },
  { pattern: /\bPinterest\b/i, name: "Pinterest" },
  { pattern: /\bTikTok\b/i, name: "TikTok" },
  
  // Messaging apps
  { pattern: /\bSlack\b/i, name: "Slack" },
  { pattern: /\bDiscord\b/i, name: "Discord" },
  { pattern: /\bTelegram\b/i, name: "Telegram" },
  { pattern: /\bLine\b/i, name: "Line" },
  { pattern: /WeChat|MicroMessenger/i, name: "WeChat" },
  { pattern: /\bWhatsApp\b/i, name: "WhatsApp" },
  { pattern: /\bMessenger\b/i, name: "Messenger" },
  
  // Productivity apps
  { pattern: /\bNotion\b/i, name: "Notion" },
  { pattern: /\bTeams\b/i, name: "Microsoft Teams" },
  { pattern: /\bZoom\b/i, name: "Zoom" },
  
  // iOS WebView indicators (when not Safari)
  // Note: We check for iOS but NOT Safari/Chrome, which indicates a WebView
  { pattern: /iPhone.*AppleWebKit(?!.*Safari)/i, name: "iOS In-App Browser" },
  { pattern: /iPad.*AppleWebKit(?!.*Safari)/i, name: "iOS In-App Browser" },
  
  // Android WebView indicators  
  { pattern: /; wv\)/i, name: "Android WebView" },
  { pattern: /Android.*Version\/[\d.]+.*Chrome\/[\d.]+/i, name: "Android WebView" },
];

/**
 * Detects if the current browser is a WebView/in-app browser
 * 
 * @returns Detection result with isWebView flag and detected app name
 */
export function detectWebView(): WebViewDetectionResult {
  // Return safe default for SSR
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    return {
      isWebView: false,
      detectedApp: null,
      userAgent: "",
    };
  }

  const userAgent = navigator.userAgent;

  // Check against all known WebView patterns
  for (const { pattern, name } of WEBVIEW_PATTERNS) {
    if (pattern.test(userAgent)) {
      return {
        isWebView: true,
        detectedApp: name,
        userAgent,
      };
    }
  }

  // Additional heuristic: Check if standalone mode is false on iOS
  // This can indicate an in-app browser in some cases
  const isStandalone = (window.navigator as Navigator & { standalone?: boolean }).standalone;
  const isIOS = /iPhone|iPad|iPod/.test(userAgent);
  
  // If on iOS, not in standalone mode, and missing Safari identifier, likely WebView
  if (isIOS && isStandalone === false && !/Safari/i.test(userAgent)) {
    return {
      isWebView: true,
      detectedApp: "iOS In-App Browser",
      userAgent,
    };
  }

  return {
    isWebView: false,
    detectedApp: null,
    userAgent,
  };
}

/**
 * Check if the browser is a standard supported browser for Google OAuth
 */
export function isSupportedBrowser(): boolean {
  if (typeof navigator === "undefined") return true;
  
  const userAgent = navigator.userAgent;
  
  // Check for standard browsers
  const isChrome = /Chrome/i.test(userAgent) && !/Edge|Edg/i.test(userAgent);
  const isFirefox = /Firefox/i.test(userAgent);
  const isSafari = /Safari/i.test(userAgent) && !/Chrome/i.test(userAgent);
  const isEdge = /Edge|Edg/i.test(userAgent);
  
  // If it's a WebView, it's not supported regardless of browser engine
  const { isWebView } = detectWebView();
  if (isWebView) return false;
  
  return isChrome || isFirefox || isSafari || isEdge;
}


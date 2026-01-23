"use client";

import { useEffect, useState } from "react";
import { Loader2, CheckCircle, XCircle } from "lucide-react";

export default function PagerDutyOAuthCallback() {
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading');
  const [message, setMessage] = useState('Processing OAuth callback...');

  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const oauthStatus = urlParams.get('oauth');
    const oauthError = urlParams.get('error');

    if (oauthStatus === 'success') {
      setStatus('success');
      setMessage('PagerDuty connected successfully!');
      
      // Notify opener window
      if (window.opener && !window.opener.closed) {
        window.opener.postMessage(
          { type: 'pagerduty-oauth-success' },
          window.location.origin
        );
      }
      
      // Close popup after a short delay
      setTimeout(() => {
        window.close();
      }, 1000);
    } else if (oauthStatus === 'failed') {
      setStatus('error');
      setMessage(oauthError ? `OAuth failed: ${oauthError}` : 'OAuth authentication failed');
      
      // Notify opener window of failure
      if (window.opener && !window.opener.closed) {
        window.opener.postMessage(
          { 
            type: 'pagerduty-oauth-error',
            error: oauthError || 'unknown_error'
          },
          window.location.origin
        );
      }
      
      // Close popup after showing error
      setTimeout(() => {
        window.close();
      }, 3000);
    } else {
      setStatus('error');
      setMessage('Invalid callback parameters');
      
      setTimeout(() => {
        window.close();
      }, 3000);
    }
  }, []);

  return (
    <div className="flex items-center justify-center min-h-screen bg-background">
      <div className="text-center space-y-4 p-8">
        {status === 'loading' && (
          <>
            <Loader2 className="h-12 w-12 animate-spin mx-auto text-primary" />
            <p className="text-lg font-medium">{message}</p>
          </>
        )}
        {status === 'success' && (
          <>
            <CheckCircle className="h-12 w-12 mx-auto text-green-600" />
            <p className="text-lg font-medium text-green-600">{message}</p>
            <p className="text-sm text-muted-foreground">This window will close automatically...</p>
          </>
        )}
        {status === 'error' && (
          <>
            <XCircle className="h-12 w-12 mx-auto text-red-600" />
            <p className="text-lg font-medium text-red-600">{message}</p>
            <p className="text-sm text-muted-foreground">This window will close automatically...</p>
          </>
        )}
      </div>
    </div>
  );
}


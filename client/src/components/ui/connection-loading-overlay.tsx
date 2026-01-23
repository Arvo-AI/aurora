"use client";

import { useEffect } from "react";
import { useMotionValue, useSpring, useTransform, motion } from "framer-motion";
import CountUp from "@/components/ui/count-up";
import { Button } from "@/components/ui/button";

interface ConnectionLoadingOverlayProps {
  /** Whether the overlay should be visible */
  isVisible: boolean;
  /** Error message to display (if any) */
  error?: string | null;
  /** Callback when user clicks "Try Again" */
  onRetry?: () => void;
  /** Custom loading message */
  message?: string;
  /** Custom success color (default: #25c151) */
  successColor?: string;
  /** Duration of the progress animation in seconds (default: 2) */
  duration?: number;
  /** Whether to show the progress bar */
  showProgressBar?: boolean;
  /** Custom className for the overlay */
  className?: string;
}

export function ConnectionLoadingOverlay({
  isVisible,
  error,
  onRetry,
  message = "Validating connection...",
  successColor = "#25c151",
  duration = 2,
  showProgressBar = true,
  className = "",
}: ConnectionLoadingOverlayProps) {
  // Progress animation using same spring config as CountUp
  const damping = 20 + 40 * (1 / duration);
  const stiffness = 100 * (1 / duration);
  
  const progressMotionValue = useMotionValue(0);
  const progressSpring = useSpring(progressMotionValue, { damping, stiffness });
  const progressWidth = useTransform(progressSpring, (value) => `${value}%`);

  useEffect(() => {
    if (isVisible && !error) {
      progressMotionValue.set(0);
      setTimeout(() => {
        progressMotionValue.set(100);
      }, 10);
    } else {
      progressMotionValue.set(0);
    }
  }, [isVisible, error, progressMotionValue]);

  if (!isVisible) {
    return null;
  }

  return (
    <div 
      className={`fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm ${className}`}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="w-full max-w-md p-8 border rounded-lg bg-card shadow-lg">
        <div className="flex flex-col items-center gap-6">
          <div className="text-center">
            {error ? (
              <>
                <div className="mb-4 p-3 rounded-lg bg-destructive/10 border border-destructive/20">
                  <p className="text-sm font-medium text-destructive">
                    {error}
                  </p>
                </div>
                {onRetry && (
                  <Button
                    onClick={onRetry}
                    className="px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90 transition-colors"
                  >
                    Try Again
                  </Button>
                )}
              </>
            ) : (
              <>
                <p className="text-sm font-medium text-muted-foreground mb-4">
                  {message}
                </p>
                <div 
                  className="text-5xl font-bold"
                  style={{ color: successColor }}
                >
                  <CountUp to={100} duration={duration} startWhen={isVisible && !error} />%
                </div>
              </>
            )}
          </div>
          {!error && showProgressBar && (
            <div className="relative w-full">
              <div className="h-3 bg-muted rounded-full overflow-hidden">
                <motion.div 
                  className="h-full bg-gradient-to-r"
                  style={{ 
                    width: progressWidth,
                    background: `linear-gradient(to right, ${successColor}, ${successColor}dd)`
                  }}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


"use client";

import { useEffect } from "react";
import { Check } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { getEnv } from "@/lib/env";

interface UpgradeModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const features = [
  "Multiple members per organization",
  "Dedicated engineering support",
  "Continuous feature development",
  "10x usage limits",
];

export default function UpgradeModal({ open, onOpenChange }: Readonly<UpgradeModalProps>) {
  const bookingUrl = getEnv("NEXT_PUBLIC_UPGRADE_BOOKING_URL");

  useEffect(() => {
    if (open) {
      fetch("/api/orgs/track", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: "upgrade_prompt_viewed" }),
      }).catch(() => {});
    }
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader className="pb-4 border-b">
          <DialogTitle className="text-xl">Upgrade to Enterprise</DialogTitle>
          <DialogDescription>
            Unlock the full potential of Aurora for your team.
          </DialogDescription>
        </DialogHeader>
        <p className="text-sm font-medium pt-1">What you get:</p>
        <ul className="space-y-3 pb-2">
          {features.map((f) => (
            <li key={f} className="flex items-center gap-3 text-sm">
              <Check className="h-4 w-4 flex-shrink-0 text-green-500" />
              {f}
            </li>
          ))}
        </ul>
        {bookingUrl && (
          <div className="border-t pt-4">
            <Button
              className="w-full"
              onClick={() => window.open(bookingUrl, "_blank", "noopener,noreferrer")}
            >
              Book a meeting
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

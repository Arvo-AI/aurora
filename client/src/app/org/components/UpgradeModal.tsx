"use client";

import { Check } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

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

export default function UpgradeModal({ open, onOpenChange }: UpgradeModalProps) {
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
              <Check className="h-4 w-4 flex-shrink-0" />
              {f}
            </li>
          ))}
        </ul>
        <div className="border-t pt-4">
          <Button
            className="w-full"
            onClick={() => window.open("https://cal.com/arvo-ai?ref=999998", "_blank")}
          >
            Book a meeting
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

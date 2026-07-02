"use client";

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface UpgradeModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export default function UpgradeModal({ open, onOpenChange }: UpgradeModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Upgrade to Enterprise</DialogTitle>
        </DialogHeader>
        <ul className="text-sm text-muted-foreground space-y-1.5 mt-2">
          <li className="flex items-center gap-2">
            <span className="h-1 w-1 rounded-full bg-foreground" />
            Multiple members per org
          </li>
          <li className="flex items-center gap-2">
            <span className="h-1 w-1 rounded-full bg-foreground" />
            Dev support
          </li>
        </ul>
        <Button
          className="w-full mt-4"
          onClick={() => window.open("https://cal.com/arvo-ai?ref=999998", "_blank")}
        >
          Book a meeting
        </Button>
      </DialogContent>
    </Dialog>
  );
}

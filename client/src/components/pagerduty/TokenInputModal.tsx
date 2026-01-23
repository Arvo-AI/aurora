"use client";

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { AlertCircle } from "lucide-react";

interface TokenInputModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (token: string) => Promise<void>;
  title: string;
  description: string;
  submitLabel?: string;
}

export function TokenInputModal({
  open,
  onOpenChange,
  onSubmit,
  title,
  description,
  submitLabel = "Update Token",
}: TokenInputModalProps) {
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token.trim()) return;

    setLoading(true);
    setError(null);

    try {
      await onSubmit(token.trim());
      setToken("");
      onOpenChange(false);
    } catch (err) {
      let errorMessage = "Failed to update token";
      if (err instanceof Error) {
        errorMessage = err.message;
        // Try to extract clean message if it's JSON
        try {
          const parsed = JSON.parse(err.message);
          if (parsed.message) errorMessage = parsed.message;
          else if (parsed.error) errorMessage = parsed.error;
        } catch {
          // Not JSON, use the message as-is
        }
      } else if (typeof err === 'string') {
        try {
          const parsed = JSON.parse(err);
          errorMessage = parsed.message || parsed.error || errorMessage;
        } catch {
          errorMessage = err;
        }
      }
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const handleOpenChange = (newOpen: boolean) => {
    if (!loading) {
      if (!newOpen) {
        setToken("");
        setError(null);
      }
      onOpenChange(newOpen);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          <div className="space-y-4 py-4">
            {error && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="modal-token">API Token</Label>
              <Input
                id="modal-token"
                type="password"
                placeholder="Paste your new PagerDuty API token"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                required
                autoFocus
              />
              <p className="text-xs text-muted-foreground">
                The new token will replace the existing one after validation.
              </p>
            </div>

            <div className="p-3 bg-muted border border-border rounded text-xs">
              <p className="text-muted-foreground">
                Aurora will validate the new token before replacing the old one. If validation fails, your existing connection remains active.
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => handleOpenChange(false)}
              disabled={loading}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={loading || !token.trim()}>
              {loading ? "Validating…" : submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}


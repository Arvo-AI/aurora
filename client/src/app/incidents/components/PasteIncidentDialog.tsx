'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { useToast } from '@/hooks/use-toast';
import { SAMPLE_INCIDENT_EVIDENCE, SAMPLE_INCIDENT_TITLE } from '../sampleIncident';

// Mirrors PASTED_EVIDENCE_MAX in server/chat/background/rca_prompt_builder.py.
const MAX_EVIDENCE_CHARS = 50_000;

interface PasteIncidentDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Lets a user with nothing connected start an investigation from text they
 * already have: an alert payload, a log excerpt, a CI failure.
 */
export default function PasteIncidentDialog({ open, onOpenChange }: PasteIncidentDialogProps) {
  const router = useRouter();
  const { toast } = useToast();
  const [title, setTitle] = useState('');
  const [evidence, setEvidence] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const trimmed = evidence.trim();
  const tooLong = trimmed.length > MAX_EVIDENCE_CHARS;
  const canSubmit = trimmed.length > 0 && !tooLong && !submitting;

  const fillSample = () => {
    setTitle(SAMPLE_INCIDENT_TITLE);
    setEvidence(SAMPLE_INCIDENT_EVIDENCE);
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const res = await fetch('/api/incidents/trigger-rca', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title.trim(),
          issue_description: title.trim() || 'Investigate the pasted evidence.',
          evidence: trimmed,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!data.incident_id) {
        throw new Error(data.error || 'Could not start the investigation');
      }
      if (data.error) {
        toast({ title: 'Investigation did not start', description: data.error, variant: 'destructive' });
      }
      onOpenChange(false);
      setTitle('');
      setEvidence('');
      router.push(`/incidents/${data.incident_id}`);
    } catch (err) {
      toast({
        title: 'Could not start the investigation',
        description: err instanceof Error ? err.message : 'Please try again.',
        variant: 'destructive',
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Investigate a pasted incident</DialogTitle>
          <DialogDescription>
            Paste an alert payload, a log excerpt or a CI failure. Aurora investigates it
            with nothing connected.
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={e => { e.preventDefault(); handleSubmit(); }}
          className="space-y-3"
        >
          <Input
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder="Title (optional), e.g. checkout-api 5xx spike"
            maxLength={200}
            aria-label="Incident title"
          />
          <Textarea
            value={evidence}
            onChange={e => setEvidence(e.target.value)}
            placeholder="Paste the alert JSON, logs, deploy output, stack trace…"
            className="h-64 font-mono text-xs md:text-xs resize-none"
            aria-label="Pasted evidence"
            autoFocus
          />
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <button type="button" onClick={fillSample} className="underline underline-offset-4 hover:text-foreground">
              Nothing to paste? Use a sample incident
            </button>
            <span className={tooLong ? 'text-destructive' : undefined}>
              {trimmed.length.toLocaleString()} / {MAX_EVIDENCE_CHARS.toLocaleString()}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            The text is stored with this incident in your Aurora database and sent to your
            configured LLM provider. No credentials are requested, so remove secrets before pasting.
          </p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)} disabled={submitting}>
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit}>
              {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Investigate
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

'use client';

import { useState, useEffect, ReactNode } from 'react';
import { ThumbsUp, ThumbsDown, Loader2, LucideIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  incidentFeedbackService,
  FeedbackType,
  IncidentFeedback as FeedbackData,
} from '@/lib/services/incident-feedback';
import { useToast } from '@/hooks/use-toast';

interface IncidentFeedbackProps {
  incidentId: string;
  existingFeedback?: FeedbackData | null;
}

interface FeedbackButtonProps {
  icon: LucideIcon;
  tooltip: string;
  onClick?: () => void;
  disabled?: boolean;
  isLoading?: boolean;
  isSelected?: boolean;
  variant: 'helpful' | 'not_helpful';
}

function FeedbackButton({
  icon: Icon,
  tooltip,
  onClick,
  disabled,
  isLoading,
  isSelected,
  variant,
}: FeedbackButtonProps): ReactNode {
  const colorClasses = variant === 'helpful'
    ? { selected: 'bg-green-500/20 text-green-400', hover: 'text-zinc-500 hover:text-green-400 hover:bg-green-500/10' }
    : { selected: 'bg-red-500/20 text-red-400', hover: 'text-zinc-500 hover:text-red-400 hover:bg-red-500/10' };

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClick}
            disabled={disabled}
            className={`h-8 w-8 ${isSelected ? colorClasses.selected : colorClasses.hover}`}
          >
            {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Icon className="w-4 h-4" />}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="bg-zinc-800 text-zinc-100 border-zinc-700">
          {tooltip}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

type FeedbackState = 'idle' | 'selecting' | 'submitting' | 'submitted';

export default function IncidentFeedback({
  incidentId,
  existingFeedback,
}: IncidentFeedbackProps): ReactNode {
  const { toast } = useToast();
  const [state, setState] = useState<FeedbackState>(existingFeedback ? 'submitted' : 'idle');
  const [selectedType, setSelectedType] = useState<FeedbackType | null>(
    existingFeedback?.feedbackType || null
  );
  const [comment, setComment] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [feedback, setFeedback] = useState<FeedbackData | null>(existingFeedback || null);

  // Fetch existing feedback on mount if not provided
  useEffect(() => {
    if (!existingFeedback && incidentId) {
      incidentFeedbackService.getFeedback(incidentId)
        .then((data) => {
          if (data) {
            setFeedback(data);
            setSelectedType(data.feedbackType);
            setState('submitted');
          }
        })
        .catch((err) => {
          console.error('Error fetching feedback:', err);
        });
    }
  }, [incidentId, existingFeedback]);

  const handleThumbsClick = (type: FeedbackType) => {
    if (state === 'submitted') return;

    setSelectedType(type);
    if (type === 'helpful') {
      // Submit immediately for helpful
      handleSubmit(type);
    } else {
      // Show comment field for not helpful
      setState('selecting');
    }
  };

  const handleSubmit = async (type?: FeedbackType, skipComment?: boolean) => {
    const feedbackType = type || selectedType;
    if (!feedbackType) return;

    setIsLoading(true);
    setState('submitting');

    try {
      await incidentFeedbackService.submitFeedback(
        incidentId,
        feedbackType,
        skipComment ? undefined : comment
      );

      setFeedback({
        id: '',
        feedbackType,
        comment: skipComment ? undefined : comment,
        createdAt: new Date().toISOString(),
      });
      setState('submitted');

      toast({
        title: 'Feedback submitted',
        description: feedbackType === 'helpful'
          ? 'Thanks! Aurora will learn from this analysis.'
          : 'Thanks for your feedback.',
      });
    } catch (err) {
      console.error('Error submitting feedback:', err);
      const message = err instanceof Error ? err.message : 'Failed to submit feedback';

      if (message.includes('Aurora Learn is disabled')) {
        toast({
          title: 'Aurora Learn is disabled',
          description: 'Enable Aurora Learn in Settings > Knowledge Base to provide feedback.',
          variant: 'destructive',
        });
      } else {
        toast({
          title: 'Failed to submit feedback',
          description: message,
          variant: 'destructive',
        });
      }

      setState('idle');
      setSelectedType(null);
    } finally {
      setIsLoading(false);
    }
  };

  const feedbackType = feedback?.feedbackType || selectedType;

  // Selecting not helpful (with comment)
  if (state === 'selecting' && selectedType === 'not_helpful') {
    return (
      <div className="space-y-3">
        <div className="flex items-center gap-1">
          <FeedbackButton
            icon={ThumbsUp}
            tooltip="Good analysis"
            onClick={() => handleThumbsClick('helpful')}
            disabled={isLoading}
            variant="helpful"
          />
          <FeedbackButton
            icon={ThumbsDown}
            tooltip="Bad analysis"
            disabled
            isSelected
            variant="not_helpful"
          />
        </div>

        <div className="space-y-2">
          <label className="text-xs text-zinc-400">
            Optional: What could be improved?
          </label>
          <Textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="The root cause was incorrect because..."
            className="min-h-[80px] bg-zinc-900 border-zinc-700 text-sm"
            maxLength={2000}
          />
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handleSubmit(undefined, true)}
              disabled={isLoading}
              className="text-zinc-400"
            >
              Skip
            </Button>
            <Button
              size="sm"
              onClick={() => handleSubmit()}
              disabled={isLoading}
              className="bg-zinc-700 hover:bg-zinc-600"
            >
              {isLoading ? (
                <>
                  <Loader2 className="w-4 h-4 mr-1.5 animate-spin" />
                  Submitting...
                </>
              ) : (
                'Submit'
              )}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  // Default and submitted states - just show icons
  const isSubmitted = state === 'submitted';

  return (
    <div className="flex items-center gap-1">
      <FeedbackButton
        icon={ThumbsUp}
        tooltip="Good analysis"
        onClick={() => handleThumbsClick('helpful')}
        disabled={isSubmitted || isLoading}
        isLoading={isLoading && selectedType === 'helpful'}
        isSelected={isSubmitted && feedbackType === 'helpful'}
        variant="helpful"
      />
      <FeedbackButton
        icon={ThumbsDown}
        tooltip="Bad analysis"
        onClick={() => handleThumbsClick('not_helpful')}
        disabled={isSubmitted || isLoading}
        isSelected={isSubmitted && feedbackType === 'not_helpful'}
        variant="not_helpful"
      />
    </div>
  );
}

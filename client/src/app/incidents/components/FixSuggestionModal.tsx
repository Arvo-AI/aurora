'use client';

import { useState, useEffect, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import { DiffView as GitDiffView, DiffModeEnum } from '@git-diff-view/react';
import { generateDiffFile } from '@git-diff-view/file';
import '@git-diff-view/react/styles/diff-view.css';
import { Suggestion, incidentsService } from '@/lib/services/incidents';
import { useToast } from '@/hooks/use-toast';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import {
  GitBranch,
  FileCode,
  AlertTriangle,
  Loader2,
  ExternalLink,
  Check,
  X,
  Edit3,
  GitCompare,
  Code
} from 'lucide-react';

interface FixSuggestionModalProps {
  suggestion: Suggestion | null;
  isOpen: boolean;
  onClose: () => void;
  onPRCreated?: (prUrl: string) => void;
}

interface AlertBannerProps {
  variant: 'success' | 'warning' | 'error';
  children: React.ReactNode;
  action?: React.ReactNode;
}

function AlertBanner({ variant, children, action }: AlertBannerProps): JSX.Element {
  const styles = {
    success: 'bg-green-500/10 border-green-500/30',
    warning: 'bg-yellow-500/10 border-yellow-500/30',
    error: 'bg-red-500/10 border-red-500/30',
  };

  const icons = {
    success: <Check className="w-4 h-4 text-green-400" />,
    warning: <AlertTriangle className="w-4 h-4 text-yellow-400 mt-0.5 flex-shrink-0" />,
    error: <X className="w-4 h-4 text-red-400 mt-0.5 flex-shrink-0" />,
  };

  const textColors = {
    success: 'text-green-300',
    warning: 'text-yellow-300',
    error: 'text-red-300',
  };

  return (
    <div className={`flex items-center gap-2 p-3 rounded-lg border ${styles[variant]}`}>
      {icons[variant]}
      <span className={`text-sm ${textColors[variant]}`}>{children}</span>
      {action && <div className="ml-auto">{action}</div>}
    </div>
  );
}

interface FileInfoProps {
  repository: string;
  filePath: string;
}

function FileInfo({ repository, filePath }: FileInfoProps): JSX.Element {
  return (
    <div className="flex items-center gap-4 text-sm">
      <div className="flex items-center gap-2 text-zinc-400">
        <GitBranch className="w-4 h-4" />
        <span className="font-mono text-zinc-300">{repository}</span>
      </div>
      <div className="flex items-center gap-2 text-zinc-400">
        <FileCode className="w-4 h-4" />
        <span className="font-mono text-orange-300">{filePath}</span>
      </div>
    </div>
  );
}

type ViewMode = 'diff' | 'edit' | 'suggested';

interface ContentEditorProps {
  originalContent: string;
  suggestedContent: string;
  viewMode: ViewMode;
  onContentChange: (content: string) => void;
  onViewModeChange: (mode: ViewMode) => void;
  disabled: boolean;
  filePath?: string;
}

interface DiffViewProps {
  oldContent: string;
  newContent: string;
  fileName?: string;
}

function DiffView({ oldContent, newContent, fileName }: DiffViewProps): JSX.Element {
  const diffFile = useMemo(() => {
    // Normalize content to avoid false diffs from trailing whitespace/newlines
    const normalize = (content: string) => {
      return content
        .split('\n')
        .map(line => line.trimEnd())
        .join('\n')
        .trim() + '\n';
    };

    const ext = fileName?.split('.').pop() || 'txt';
    const file = generateDiffFile(
      fileName || 'original',
      normalize(oldContent || ''),
      fileName || 'modified',
      normalize(newContent || ''),
      ext,
      ext
    );
    file.initTheme('dark');
    file.init();
    file.buildSplitDiffLines();
    file.buildUnifiedDiffLines();
    return file;
  }, [oldContent, newContent, fileName]);

  return (
    <div className="rounded-lg border border-zinc-800 overflow-hidden max-h-[60vh] overflow-y-auto">
      <GitDiffView
        diffFile={diffFile}
        diffViewMode={DiffModeEnum.Unified}
        diffViewTheme="dark"
        diffViewHighlight={true}
        diffViewWrap={true}
      />
    </div>
  );
}

function ContentEditor({
  originalContent,
  suggestedContent,
  viewMode,
  onContentChange,
  onViewModeChange,
  disabled,
  filePath,
}: ContentEditorProps): JSX.Element {
  // Detect file type from path for syntax highlighting hint
  const fileExtension = filePath?.split('.').pop() || '';

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label className="text-xs font-medium text-zinc-500 uppercase tracking-wider">
          {viewMode === 'edit' ? 'Edit Fix' : viewMode === 'diff' ? 'Changes' : 'Suggested Fix'}
        </label>
        {!disabled && (
          <div className="flex items-center gap-1">
            <Button
              variant={viewMode === 'diff' ? 'secondary' : 'ghost'}
              size="sm"
              onClick={() => onViewModeChange('diff')}
              className={viewMode === 'diff' ? 'bg-zinc-700' : 'text-zinc-400 hover:text-white'}
            >
              <GitCompare className="w-4 h-4 mr-1" />
              Diff
            </Button>
            <Button
              variant={viewMode === 'suggested' ? 'secondary' : 'ghost'}
              size="sm"
              onClick={() => onViewModeChange('suggested')}
              className={viewMode === 'suggested' ? 'bg-zinc-700' : 'text-zinc-400 hover:text-white'}
            >
              <Code className="w-4 h-4 mr-1" />
              Code
            </Button>
            <Button
              variant={viewMode === 'edit' ? 'secondary' : 'ghost'}
              size="sm"
              onClick={() => onViewModeChange('edit')}
              className={viewMode === 'edit' ? 'bg-zinc-700' : 'text-zinc-400 hover:text-white'}
            >
              <Edit3 className="w-4 h-4 mr-1" />
              Edit
            </Button>
          </div>
        )}
      </div>

      {viewMode === 'edit' ? (
        <textarea
          value={suggestedContent}
          onChange={(e) => onContentChange(e.target.value)}
          className="w-full h-80 p-4 rounded-lg bg-zinc-950 border border-zinc-800 text-sm font-mono text-zinc-300 focus:outline-none focus:border-orange-500 resize-none"
          spellCheck={false}
        />
      ) : viewMode === 'diff' ? (
        <DiffView oldContent={originalContent} newContent={suggestedContent} fileName={filePath} />
      ) : (
        <div className="relative h-80 overflow-auto rounded-lg bg-zinc-950 border border-zinc-800">
          <pre className="p-4 text-sm font-mono text-zinc-300 whitespace-pre-wrap">
            {suggestedContent}
          </pre>
        </div>
      )}

      {fileExtension && (
        <div className="text-xs text-zinc-500">
          File type: <span className="text-zinc-400">.{fileExtension}</span>
        </div>
      )}
    </div>
  );
}

export default function FixSuggestionModal({
  suggestion,
  isOpen,
  onClose,
  onPRCreated,
}: FixSuggestionModalProps): JSX.Element | null {
  const { toast } = useToast();
  const [isCreatingPR, setIsCreatingPR] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editedContent, setEditedContent] = useState('');
  const [viewMode, setViewMode] = useState<ViewMode>('diff');
  const [prCreated, setPrCreated] = useState(false);
  const [createdPrUrl, setCreatedPrUrl] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen && suggestion) {
      setEditedContent(suggestion.userEditedContent || suggestion.suggestedContent || '');
      setPrCreated(!!suggestion.prUrl);
      setCreatedPrUrl(suggestion.prUrl || null);
      setViewMode('diff');
      setError(null);
    }
  }, [isOpen, suggestion]);

  if (!suggestion || suggestion.type !== 'fix') {
    return null;
  }

  // Capture suggestion ID for use in async functions (TypeScript narrowing workaround)
  const suggestionId = suggestion.id;
  // Track if user has edited the content from what was last saved
  const savedContent = suggestion.userEditedContent || suggestion.suggestedContent || '';
  const hasChanges = editedContent !== savedContent;
  const isEditing = viewMode === 'edit';

  async function handleSaveEdit(): Promise<void> {
    if (!hasChanges) {
      setViewMode('diff');
      return;
    }

    try {
      await incidentsService.updateFixSuggestion(suggestionId, editedContent);
      setViewMode('diff');
      setError(null);
      toast({
        title: 'Changes saved',
        description: 'Your edits have been saved successfully.',
      });
    } catch {
      setError('Failed to save changes');
      toast({
        title: 'Error',
        description: 'Failed to save changes. Please try again.',
        variant: 'destructive',
      });
    }
  }

  async function handleCreatePR(): Promise<void> {
    setIsCreatingPR(true);
    setError(null);

    try {
      if (hasChanges) {
        await incidentsService.updateFixSuggestion(suggestionId, editedContent);
      }

      const result = await incidentsService.applyFixSuggestion(suggestionId, {
        useEditedContent: true,
      });

      if (result.success) {
        setPrCreated(true);
        setCreatedPrUrl(result.prUrl || null);
        if (result.prUrl) {
          onPRCreated?.(result.prUrl);
        }
        toast({
          title: 'Pull Request Created',
          description: result.prUrl
            ? 'Your PR has been created successfully. Click the link to view it.'
            : 'PR created but URL not available. Check your GitHub repository.',
        });
      } else {
        const errorMsg = result.error || 'Failed to create PR';
        setError(errorMsg);
        toast({
          title: 'Failed to create PR',
          description: errorMsg,
          variant: 'destructive',
        });
      }
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to create PR';
      setError(errorMsg);
      toast({
        title: 'Error',
        description: errorMsg,
        variant: 'destructive',
      });
    } finally {
      setIsCreatingPR(false);
    }
  }

  function handleDialogChange(open: boolean): void {
    if (!open) {
      onClose();
    }
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleDialogChange}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-hidden flex flex-col bg-zinc-900 border-zinc-700">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-white">
            <FileCode className="w-5 h-5 text-green-400" />
            {suggestion.title}
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4 pr-2">
          <div className="text-sm text-zinc-300 prose prose-invert prose-sm max-w-none">
            <ReactMarkdown>{suggestion.description}</ReactMarkdown>
          </div>

          <FileInfo repository={suggestion.repository || ''} filePath={suggestion.filePath || ''} />

          <div className="flex items-center gap-3">
            <span className="px-2 py-1 text-xs rounded border bg-green-500/20 text-green-400 border-green-500/30">
              Code Fix
            </span>
            <span className={`px-2 py-1 text-xs rounded border ${incidentsService.getRiskColor(suggestion.risk)}`}>
              {suggestion.risk} risk
            </span>
          </div>

          {prCreated && createdPrUrl && (
            <AlertBanner
              variant="success"
              action={
                <a
                  href={createdPrUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1 text-sm text-green-400 hover:text-green-300"
                >
                  View PR <ExternalLink className="w-3 h-3" />
                </a>
              }
            >
              PR created successfully!
            </AlertBanner>
          )}

          <ContentEditor
            originalContent={suggestion.originalContent || ''}
            suggestedContent={editedContent}
            viewMode={viewMode}
            onContentChange={setEditedContent}
            onViewModeChange={setViewMode}
            disabled={prCreated}
            filePath={suggestion.filePath}
          />

          <AlertBanner variant="warning">
            <span className="text-xs">
              Review the suggested changes carefully. A pull request will be created for your review before merging.
            </span>
          </AlertBanner>

          {error && (
            <AlertBanner variant="error">
              <span className="text-xs">{error}</span>
            </AlertBanner>
          )}
        </div>

        <DialogFooter className="flex gap-2 sm:gap-2 pt-4 border-t border-zinc-800">
          {isEditing && hasChanges && (
            <Button
              variant="outline"
              onClick={handleSaveEdit}
              className="border-zinc-700 hover:bg-zinc-800"
            >
              <Check className="w-4 h-4 mr-2" />
              Save Changes
            </Button>
          )}
          <Button
            variant="outline"
            onClick={onClose}
            className="border-zinc-700 hover:bg-zinc-800"
          >
            Close
          </Button>
          {!prCreated && (
            <Button
              onClick={handleCreatePR}
              disabled={isCreatingPR || isEditing}
              className="bg-green-600 hover:bg-green-700 text-white"
            >
              {isCreatingPR ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  Creating PR...
                </>
              ) : (
                <>
                  <GitBranch className="w-4 h-4 mr-2" />
                  Create Pull Request
                </>
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

'use client';
import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { ScrollArea } from '@/components/ui/scroll-area';

interface Project {
  projectId: string;
  name: string;
}

interface ProjectSelectionModalProps {
  open: boolean;
  projects: Project[];
  onSelect: (selectedIds: string[]) => void;
  onCancel: () => void;
}

export function ProjectSelectionModal({ open, projects, onSelect, onCancel }: ProjectSelectionModalProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Reset selection when modal closes
  useEffect(() => {
    if (!open) {
      setSelected(new Set());
    }
  }, [open]);

  const toggleProject = (id: string) => {
    const newSelected = new Set(selected);
    if (newSelected.has(id)) {
      newSelected.delete(id);
    } else if (newSelected.size < 5) {
      newSelected.add(id);
    }
    setSelected(newSelected);
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onCancel()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Too Many Projects to Index</DialogTitle>
          <DialogDescription>
            You have {projects.length} eligible projects. Please select up to 5 projects to index.
            <span className="block mt-1 font-medium">Selected: {selected.size}/5</span>
          </DialogDescription>
        </DialogHeader>
        <ScrollArea className="h-[400px] pr-4">
          <div className="space-y-2">
            {projects.map((p) => (
              <div key={p.projectId} className="flex items-center space-x-2 p-2 hover:bg-accent rounded">
                <Checkbox
                  id={p.projectId}
                  checked={selected.has(p.projectId)}
                  onCheckedChange={() => toggleProject(p.projectId)}
                  disabled={!selected.has(p.projectId) && selected.size >= 5}
                />
                <label htmlFor={p.projectId} className="text-sm cursor-pointer flex-1">
                  {p.name} <span className="text-muted-foreground">({p.projectId})</span>
                </label>
              </div>
            ))}
          </div>
        </ScrollArea>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>Cancel</Button>
          <Button onClick={() => onSelect(Array.from(selected))} disabled={selected.size === 0}>
            Index {selected.size} Project{selected.size !== 1 ? 's' : ''}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}


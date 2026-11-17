import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';

export interface Finding {
  severity: 'HIGH' | 'MEDIUM' | 'LOW';
  file_path: string;
  line: number | null;
  title: string;
  explanation: string;
}

export interface Verdict {
  verdict: 'SAFE' | 'RISKY';
  summary: string;
  findings: Finding[];
}

export function VerdictView({ verdict, note }: { verdict: Verdict; note?: string }) {
  const risky = verdict.verdict === 'RISKY';
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {risky ? (
          <AlertTriangle className="h-5 w-5 text-red-500" />
        ) : (
          <CheckCircle2 className="h-5 w-5 text-green-500" />
        )}
        <Badge variant={risky ? 'destructive' : 'secondary'}>{verdict.verdict}</Badge>
        {note && <span className="text-xs text-muted-foreground">{note}</span>}
      </div>
      <p className="text-sm whitespace-pre-wrap">{verdict.summary}</p>
      {verdict.findings.length > 0 && (
        <ul className="space-y-3">
          {verdict.findings.map((f, i) => (
            <li key={`${f.file_path}:${f.line ?? ''}:${i}`} className="rounded-md border border-border p-3">
              <div className="flex items-center gap-2 text-sm">
                <Badge variant="outline">{f.severity}</Badge>
                <span className="font-medium">{f.title}</span>
              </div>
              <p className="text-xs text-muted-foreground mt-1 font-mono">
                {f.file_path}
                {f.line ? `:${f.line}` : ''}
              </p>
              <p className="text-sm mt-2 whitespace-pre-wrap">{f.explanation}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

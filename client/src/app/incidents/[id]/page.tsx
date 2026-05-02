'use client';

import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import { incidentsService, Incident, StreamingThought } from '@/lib/services/incidents';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { ArrowLeft, AlertTriangle, GitMerge } from 'lucide-react';
import { useAuth } from '@/hooks/useAuthHooks';
import { canWrite } from '@/lib/roles';

import IncidentCard from '../components/IncidentCard';
import ThoughtsPanel from '../components/ThoughtsPanel';

const STALE_POLL_MS = 5 * 60 * 1000;

export default function IncidentDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { role } = useAuth();
  const [incident, setIncident] = useState<Incident | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showThoughts, setShowThoughts] = useState(false);
  const [thoughts, setThoughts] = useState<StreamingThought[]>([]);
  const seenThoughtIdsRef = useRef<Set<string>>(new Set());
  const userClosedThoughtsRef = useRef<boolean>(false);
  const pollStartRef = useRef<number>(0);

  const applyIncidentData = useCallback((data: Incident) => {
    const newThoughts = data.streamingThoughts || [];
    const unseenThoughts = newThoughts.filter(t => {
      if (seenThoughtIdsRef.current.has(t.id)) return false;
      seenThoughtIdsRef.current.add(t.id);
      return true;
    });
    if (unseenThoughts.length > 0) {
      setThoughts(prev => [...prev, ...unseenThoughts]);
    }
    setIncident(data);
    if (data.status === 'investigating' && !userClosedThoughtsRef.current) {
      setShowThoughts(true);
    }
  }, []);

  // Single fetch + poll loop — one effect, no duplicates
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;

    const fetchAndSchedule = async (isInitial: boolean) => {
      if (!active || !params.id) return;
      try {
        const data = await incidentsService.getIncident(params.id as string);
        if (!active) return;
        if (!data) {
          if (isInitial) setError('Incident not found');
          return;
        }
        applyIncidentData(data);

        const needsPoll = data.status === 'investigating' || data.auroraStatus === 'summarizing';
        if (needsPoll && active) {
          if (!pollStartRef.current) pollStartRef.current = Date.now();
          if (Date.now() - pollStartRef.current > STALE_POLL_MS) {
            setIncident(prev => prev ? { ...prev, auroraStatus: 'error' as const } : prev);
            return;
          }
          timer = setTimeout(() => fetchAndSchedule(false), 1000);
        } else {
          pollStartRef.current = 0;
        }
      } catch (e) {
        if (!active) return;
        if (isInitial) {
          setError('Failed to load incident');
          console.error('Failed to load incident:', e instanceof Error ? e.message : 'Unknown error');
        }
      } finally {
        if (isInitial && active) setLoading(false);
      }
    };

    fetchAndSchedule(true);
    return () => { active = false; clearTimeout(timer); };
  }, [params.id, applyIncidentData]);

  if (loading) {
    return (
      <div className="min-h-screen bg-background p-6">
        <div className="max-w-4xl mx-auto space-y-6">
          <Skeleton className="h-8 w-48 bg-zinc-800" />
          <Skeleton className="h-32 w-full bg-zinc-800 rounded-xl" />
          <Skeleton className="h-64 w-full bg-zinc-800 rounded-xl" />
          <Skeleton className="h-48 w-full bg-zinc-800 rounded-xl" />
        </div>
      </div>
    );
  }

  if (error || !incident) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="text-center">
          <AlertTriangle className="w-12 h-12 text-red-500 mx-auto mb-4" />
          <p className="text-red-400 mb-4">{error || 'Incident not found'}</p>
          <Button
            variant="outline"
            onClick={() => router.push('/incidents')}
          >
            <ArrowLeft className="w-4 h-4 mr-2" />
            Back to incidents
          </Button>
        </div>
      </div>
    );
  }

  const duration = incidentsService.formatDuration(
    incident.startedAt
  );

  const refreshIncident = async () => {
    try {
      const data = await incidentsService.getIncident(params.id as string);
      if (data) {
        setIncident(data);
      }
    } catch (e) {
      console.error('Failed to refresh incident:', e);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      {/* Merged incident banner */}
      {incident.status === 'merged' && incident.mergedIntoIncidentId && (
        <div className="bg-zinc-900/50 border-b border-zinc-800 px-6 py-4">
          <div className="max-w-5xl mx-auto flex items-center gap-3 text-zinc-400">
            <GitMerge className="w-5 h-5 text-zinc-500" />
            <div>
              <p className="text-sm">
                This incident was merged into{' '}
                <Link 
                  href={`/incidents/${incident.mergedIntoIncidentId}`}
                  className="text-blue-400 hover:text-blue-300 font-medium"
                >
                  "{incident.mergedIntoTitle || 'another incident'}"
                </Link>
              </p>
              <p className="text-xs text-zinc-600 mt-1">
                Its RCA investigation has been stopped. View the main incident to see the combined analysis.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Sticky header with back button - full width, always on top */}
      <div className="border-b border-zinc-800/50 bg-background/95 backdrop-blur-sm sticky top-0 z-30">
        <div className="px-6 py-3">
          <Link href="/incidents" className="inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white transition-colors">
            <ArrowLeft className="w-4 h-4" />
            Back to incidents
          </Link>
        </div>
      </div>

      <div className="flex">
        {/* Main content area */}
        <div 
          className={`flex-1 transition-all duration-300 ${showThoughts ? 'mr-[400px]' : ''}`}
          onClick={() => {
            if (showThoughts) {
              setShowThoughts(false);
              userClosedThoughtsRef.current = true;
            }
          }}
        >
          {/* Main content - wider, more breathing room */}
          <div className="max-w-5xl mx-auto px-8 py-8">
            <IncidentCard
              incident={incident}
              duration={duration}
              showThoughts={showThoughts}
              onToggleThoughts={() => {
                const newValue = !showThoughts;
                setShowThoughts(newValue);
                // Track if user manually closed the panel
                if (!newValue) {
                  userClosedThoughtsRef.current = true;
                } else {
                  // Reset when user opens it again
                  userClosedThoughtsRef.current = false;
                }
              }}
              citations={incident.citations}
              onRefresh={refreshIncident}
            />
          </div>
        </div>

        {/* Thoughts Panel - Right sidebar with tabs */}
        <ThoughtsPanel
          thoughts={thoughts}
          incident={incident}
          isVisible={showThoughts}
          canInteract={canWrite(role)}
        />
      </div>
    </div>
  );
}

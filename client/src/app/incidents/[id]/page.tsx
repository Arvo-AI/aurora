'use client';

import { useEffect, useState, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import { incidentsService, Incident, StreamingThought } from '@/lib/services/incidents';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { ArrowLeft, AlertTriangle } from 'lucide-react';

import IncidentCard from '../components/IncidentCard';
import ThoughtsPanel from '../components/ThoughtsPanel';

export default function IncidentDetailPage() {
  const params = useParams();
  const router = useRouter();
  const [incident, setIncident] = useState<Incident | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showThoughts, setShowThoughts] = useState(false);
  const [thoughts, setThoughts] = useState<StreamingThought[]>([]);
  const seenThoughtIdsRef = useRef<Set<string>>(new Set());
  const userClosedThoughtsRef = useRef<boolean>(false);

  useEffect(() => {

    let isMounted = true;

    const loadIncident = async () => {
      try {
        const id = params.id as string;
        const data = await incidentsService.getIncident(id);
        if (isMounted) {
          if (!data) {
            setError('Incident not found');
          } else {
            setIncident(data);
            // Initialize seen IDs from initial load
            const initialThoughts = data.streamingThoughts || [];
            initialThoughts.forEach((thought: StreamingThought) => {
              seenThoughtIdsRef.current.add(thought.id);
            });
            setThoughts(initialThoughts);
            if (data.status === 'investigating' && !userClosedThoughtsRef.current) {
              setShowThoughts(true);
            }
          }
        }
      } catch (e) {
        if (isMounted) {
          setError('Failed to load incident');
          console.error('Failed to load incident:', e instanceof Error ? e.message : 'Unknown error');
        }
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    };

    loadIncident();

    return () => {
      isMounted = false;
    };
  }, [params.id]);

  // Poll for incident updates
  useEffect(() => {
    let isMounted = true;

    const pollIncident = async () => {
      if (!isMounted || !params.id) return;

      try {
        const data = await incidentsService.getIncident(params.id as string);
        if (data && isMounted) {
          // Append only new thoughts that we haven't seen yet
          setThoughts(prevThoughts => {
            const newThoughts = data.streamingThoughts || [];
            // Filter to only thoughts we haven't seen
            const unseenThoughts = newThoughts.filter((thought: StreamingThought) => {
              if (seenThoughtIdsRef.current.has(thought.id)) {
                return false;
              }
              seenThoughtIdsRef.current.add(thought.id);
              return true;
            });
            
            // If we've seen all thoughts before, just return previous state
            if (unseenThoughts.length === 0) {
              return prevThoughts;
            }
            
            // Append new thoughts progressively
            return [...prevThoughts, ...unseenThoughts];
          });
          setIncident(data);

          // Only auto-open if status is investigating and user hasn't manually closed it
          if (data.status === 'investigating' && !userClosedThoughtsRef.current) {
            setShowThoughts(true);
          }
        }
      } catch (e) {
        console.error('Failed to poll incident:', e);
      }
    };

    pollIncident();
    const interval = setInterval(pollIncident, 1000); // Poll every 1 second for smoother streaming

    return () => {
      isMounted = false;
      clearInterval(interval);
    };
  }, [params.id]);

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

  return (
    <div className="min-h-screen bg-background">
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
              onExecutionStarted={() => {
                setShowThoughts(true);
                userClosedThoughtsRef.current = false;
              }}
            />
          </div>
        </div>

        {/* Thoughts Panel - Right sidebar with tabs */}
        <ThoughtsPanel
          thoughts={thoughts}
          incident={incident}
          isVisible={showThoughts}
        />
      </div>
    </div>
  );
}

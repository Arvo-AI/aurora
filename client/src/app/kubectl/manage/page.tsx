"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { ArrowLeft, Loader2, LogOut, RefreshCw, Copy, Check } from "lucide-react";
import { useToast } from "@/hooks/use-toast";

interface Connection {
  cluster_id: string;
  cluster_name: string;
  connected_at: string;
  last_heartbeat: string;
  agent_version?: string;
  status: 'active' | 'stale';
}

export default function ManageKubectlClustersPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const [deleteCommand, setDeleteCommand] = useState<string | null>(null);
  const [showCommandDialog, setShowCommandDialog] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [clusterToDelete, setClusterToDelete] = useState<{ id: string; name: string } | null>(null);
  const [commandCopied, setCommandCopied] = useState(false);

  const loadConnections = async () => {
    try {
      setLoading(true);
      const res = await fetch('/api/kubectl/connections');
      if (!res.ok) {
        throw new Error('Failed to load connections');
      }
      const data = await res.json();
      const conns = data.connections || [];
      setConnections(conns);
      // Trigger graph discovery if there are active clusters
      if (conns.some((c: Connection) => c.status === 'active')) {
        localStorage.setItem("aurora_graph_discovery_trigger", "1");
      }
    } catch (error) {
      console.error('Error loading connections:', error);
      toast({
        title: "Error",
        description: "Failed to load connected clusters",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadConnections();
  }, []);

  const handleDisconnect = async (clusterId: string, clusterName: string) => {
    setClusterToDelete({ id: clusterId, name: clusterName });
    setShowDeleteConfirm(true);
  };

  const confirmDisconnect = async () => {
    if (!clusterToDelete) return;
    
    try {
      setDisconnecting(clusterToDelete.id);
      setShowDeleteConfirm(false);
      
      const res = await fetch(`/api/kubectl/connections/${clusterToDelete.id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Failed to disconnect');
      const data = await res.json();
      if (data.delete_command) { setDeleteCommand(data.delete_command); setShowCommandDialog(true); }
      toast({ title: "Token revoked", description: `${clusterToDelete.name} token revoked` });
      await loadConnections();
      
      // Clear localStorage if no active connections remain
      const activeConns = connections.filter(c => c.cluster_id !== clusterToDelete.id && c.status === 'active');
      if (activeConns.length === 0) {
        localStorage.removeItem('isKubectlConnected');
        window.dispatchEvent(new CustomEvent('providerStateChanged'));
      }
    } catch (error) {
      console.error('Error disconnecting:', error);
      toast({ title: "Error", description: "Failed to disconnect cluster", variant: "destructive" });
    } finally {
      setDisconnecting(null);
      setClusterToDelete(null);
    }
  };

  const handleReconnect = async (clusterId: string, clusterName: string) => {
    router.push('/kubectl/auth');
  };

  const copyCommand = async () => {
    if (!deleteCommand) return;
    try {
      await navigator.clipboard.writeText(deleteCommand);
      setCommandCopied(true);
      setTimeout(() => setCommandCopied(false), 2000);
    } catch (error) {
      console.error("Failed to copy", error);
    }
  };

  const formatDate = (dateString: string) => {
    try {
      return new Date(dateString).toLocaleString();
    } catch {
      return dateString;
    }
  };

  const formatTimeAgo = (dateString: string) => {
    try {
      const date = new Date(dateString);
      const now = new Date();
      const seconds = Math.floor((now.getTime() - date.getTime()) / 1000);
      
      // Handle clock skew - if timestamp is in the future or very recent, show "just now"
      if (seconds < 5) return 'just now';
      
      // If somehow negative (clock skew), take absolute value
      const absSeconds = Math.abs(seconds);
      
      if (absSeconds < 60) return `${absSeconds}s ago`;
      const minutes = Math.floor(absSeconds / 60);
      if (minutes < 60) return `${minutes}m ago`;
      const hours = Math.floor(minutes / 60);
      if (hours < 24) return `${hours}h ago`;
      const days = Math.floor(hours / 24);
      return `${days}d ago`;
    } catch {
      return dateString;
    }
  };

  return (
    <div className="min-h-screen bg-black text-white p-8">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center gap-4 mb-8">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push('/kubectl/auth')}
            className="text-zinc-400 hover:text-white"
          >
            <ArrowLeft className="h-4 w-4 mr-2" />
            Back to Setup
          </Button>
        </div>

        <Card className="bg-zinc-950 border-zinc-800">
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="text-white text-2xl">
                  Manage Kubernetes Clusters
                </CardTitle>
                <CardDescription className="text-zinc-400 mt-2">
                  View and manage your connected kubernetes agents
                </CardDescription>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={loadConnections}
                  disabled={loading}
                  className="border-zinc-700 hover:bg-zinc-900"
                >
                  <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
                  Refresh
                </Button>
                <Button
                  variant="default"
                  size="sm"
                  onClick={() => router.push('/kubectl/auth')}
                  className="bg-white text-black hover:bg-zinc-200"
                >
                  Add Cluster
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {loading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="h-8 w-8 animate-spin text-zinc-400" />
              </div>
            ) : connections.length === 0 ? (
              <div className="text-center py-12">
                <p className="text-zinc-400 mb-4">No connected clusters found</p>
                <Button
                  variant="default"
                  onClick={() => router.push('/kubectl/auth')}
                  className="bg-white text-black hover:bg-zinc-200"
                >
                  Connect a Cluster
                </Button>
              </div>
            ) : (
              <div className="space-y-3">
                {connections.map((conn) => {
                  return (
                    <div
                      key={conn.cluster_id}
                      className="flex items-center justify-between p-4 bg-zinc-900 border border-zinc-800 rounded-lg"
                    >
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-3 mb-2">
                          <h3 className="text-white font-medium truncate">
                            {conn.cluster_name}
                          </h3>
                          <span className={`text-xs px-2 py-1 rounded ${
                            conn.status === 'active' ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'
                          }`}>
                            {conn.status === 'active' ? 'Active' : 'Stale'}
                          </span>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 text-sm text-zinc-400">
                          <div>
                            <span className="text-zinc-500">Cluster ID:</span>{' '}
                            <code className="text-xs bg-zinc-950 px-1.5 py-0.5 rounded">
                              {conn.cluster_id}
                            </code>
                          </div>
                          <div>
                            <span className="text-zinc-500">Connected:</span>{' '}
                            {formatDate(conn.connected_at)}
                          </div>
                          <div>
                            <span className="text-zinc-500">Last Heartbeat:</span>{' '}
                            {formatTimeAgo(conn.last_heartbeat)}
                          </div>
                        </div>
                        {conn.agent_version && (
                          <div className="mt-1 text-xs text-zinc-500">
                            Agent version: {conn.agent_version}
                          </div>
                        )}
                      </div>
                      <div className="flex gap-2 ml-4">
                        {conn.status === 'stale' && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => handleReconnect(conn.cluster_id, conn.cluster_name)}
                            className="border-green-700 text-green-400 hover:bg-green-950/20"
                          >
                            Reconnect
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDisconnect(conn.cluster_id, conn.cluster_name)}
                          disabled={disconnecting === conn.cluster_id}
                          className="text-red-400 hover:text-red-300 hover:bg-red-950/20"
                        >
                          {disconnecting === conn.cluster_id ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <>
                              <LogOut className="h-4 w-4 mr-2" />
                              Remove
                            </>
                          )}
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Delete Confirmation Dialog */}
        <AlertDialog open={showDeleteConfirm} onOpenChange={setShowDeleteConfirm}>
          <AlertDialogContent className="bg-zinc-950 border-zinc-800">
            <AlertDialogHeader>
              <AlertDialogTitle className="text-white">Remove Cluster?</AlertDialogTitle>
              <AlertDialogDescription className="text-zinc-400">
                This will revoke the token for <span className="font-semibold text-zinc-300">{clusterToDelete?.name}</span> and disconnect the agent. You'll need to run a command to remove the agent from your cluster.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel className="bg-zinc-900 border-zinc-800 hover:bg-zinc-800 text-white">
                Cancel
              </AlertDialogCancel>
              <AlertDialogAction
                onClick={confirmDisconnect}
                className="bg-red-600 hover:bg-red-700 text-white"
              >
                Remove Cluster
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {/* Command Dialog */}
        <Dialog open={showCommandDialog} onOpenChange={setShowCommandDialog}>
          <DialogContent className="bg-zinc-950 border-zinc-800 text-white max-w-3xl w-[min(90vw,960px)]">
            <DialogHeader>
              <DialogTitle className="text-white">Remove Agent from Cluster</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 text-zinc-400">
              <p className="text-sm break-words">
                Token revoked. Run this command to remove the agent:
              </p>
            </div>
            <div className="space-y-4">
              <div className="relative">
                <pre className="overflow-auto rounded-lg bg-zinc-900 border border-zinc-800 p-3 pr-12 text-sm font-mono text-zinc-100">
                  {deleteCommand}
                </pre>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={copyCommand}
                  className="absolute right-2 top-2 text-zinc-400 hover:text-zinc-100"
                >
                  {commandCopied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
              <p className="text-xs text-zinc-500 break-words">
                Note: Replace <code className="bg-zinc-900 px-1 py-0.5 rounded text-zinc-300">&lt;your-namespace&gt;</code> with your namespace.
              </p>
              <Button
                onClick={() => setShowCommandDialog(false)}
                className="w-full"
              >
                Done
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>
    </div>
  );
}


"use client";

import React, { useState, useMemo, useEffect } from 'react';
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Settings, User, BookOpen, FileText, Building2, Shield, DollarSign } from "lucide-react";
import { cn } from "@/lib/utils";
import { GeneralSettings } from "@/components/GeneralSettings";
import { ProfileSettings } from "@/components/ProfileSettings";
import { KnowledgeBaseSettings } from "@/components/KnowledgeBaseSettings";
import { PostmortemsSettings } from "@/components/PostmortemsSettings";
import { OrgSettings } from "@/components/OrgSettings";
import { SecuritySettings } from "@/components/SecuritySettings";
import { useUser, useAuth } from "@/hooks/useAuthHooks";
import { isAdmin } from "@/lib/roles";
import { PeriodSelector, type Period } from "@/app/monitor/components/charts";


interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type SettingsTab = 'organization' | 'general' | 'profile' | 'knowledge-base' | 'postmortems' | 'security' | 'usage';

const UsageTab = React.lazy(() => import('@/app/monitor/components/usage-tab'));

export function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>('organization');
  const [usagePeriod, setUsagePeriod] = useState<Period>('30d');
  useUser();
  const { role } = useAuth();

  const allTabs = [
    {
      id: 'organization' as SettingsTab,
      label: 'Organization',
      icon: Building2,
      description: 'Manage your organization'
    },
    {
      id: 'general' as SettingsTab,
      label: 'General',
      icon: Settings,
      description: 'General application settings'
    },
    {
      id: 'profile' as SettingsTab,
      label: 'Account Info',
      icon: User,
      description: 'Update your profile information'
    },
    {
      id: 'knowledge-base' as SettingsTab,
      label: 'Knowledge Base',
      icon: BookOpen,
      description: 'Manage documentation and context'
    },
    {
      id: 'postmortems' as SettingsTab,
      label: 'Postmortems',
      icon: FileText,
      description: 'View generated postmortems'
    },
    {
      id: 'security' as SettingsTab,
      label: 'Security',
      icon: Shield,
      description: 'Agent command policies'
    },
    {
      id: 'usage' as SettingsTab,
      label: 'Usage & Cost',
      icon: DollarSign,
      description: 'LLM usage and cost tracking',
      adminOnly: true,
    },
  ];

  const tabs = useMemo(() => {
    if (isAdmin(role)) return allTabs;
    return allTabs.filter(t => !('adminOnly' in t && t.adminOnly));
  }, [role]);

  useEffect(() => {
    if (!tabs.some(t => t.id === activeTab)) {
      setActiveTab(tabs[0]?.id ?? 'organization');
    }
  }, [tabs, activeTab]);

  const renderContent = () => {
    switch (activeTab) {
      case 'organization':
        return (
          <div className="p-6 h-full overflow-y-auto flex flex-col min-h-0">
            <h2 className="text-2xl font-bold mb-6 flex-shrink-0">Organization</h2>
            <div className="flex-1 overflow-y-auto min-h-0">
              <OrgSettings />
            </div>
          </div>
        );

      case 'general':
        return (
          <div className="p-6 h-full overflow-y-auto flex flex-col min-h-0">
            <h2 className="text-2xl font-bold mb-6 flex-shrink-0">General Settings</h2>
            <div className="flex-1 overflow-y-auto min-h-0">
              <GeneralSettings />
            </div>
          </div>
        );
      
      case 'knowledge-base':
        return (
          <div className="p-6 h-full overflow-y-auto flex flex-col min-h-0">
            <div className="flex-1 overflow-y-auto min-h-0">
              <KnowledgeBaseSettings />
            </div>
          </div>
        );

      case 'profile':
        return (
          <div className="p-6 h-full overflow-y-auto flex flex-col min-h-0">
            <h2 className="text-2xl font-bold mb-6 flex-shrink-0">Account Info</h2>
            <div className="flex-1 overflow-y-auto min-h-0">
              <ProfileSettings />
            </div>
          </div>
        );

      case 'postmortems':
        return (
          <div className="h-full overflow-y-auto">
            <PostmortemsSettings />
          </div>
        );

      case 'security':
        return (
          <div className="p-6 h-full overflow-y-auto">
            <SecuritySettings />
          </div>
        );

      case 'usage':
        if (!isAdmin(role)) return null;
        return (
          <div className="p-6 h-full overflow-y-auto flex flex-col min-h-0">
            <div className="flex items-center justify-between mb-6 flex-shrink-0">
              <h2 className="text-2xl font-bold">Usage & Cost</h2>
              <PeriodSelector value={usagePeriod} onChange={setUsagePeriod} />
            </div>
            <div className="flex-1 overflow-y-auto min-h-0 pr-4">
              <React.Suspense fallback={<div className="text-sm text-muted-foreground">Loading...</div>}>
                <UsageTab period={usagePeriod} />
              </React.Suspense>
            </div>
          </div>
        );

      default:
        return null;
    }
  };

  return (
                    <Dialog open={isOpen} onOpenChange={onClose}>
                  <DialogContent className="max-w-6xl h-[80vh] p-0 overflow-hidden">
                    <DialogTitle className="sr-only">Settings</DialogTitle>
                    <DialogDescription className="sr-only">
                      Configure your Aurora account settings, billing preferences, and profile information.
                    </DialogDescription>
                    <div className="flex h-full min-h-0">
                      {/* Left Navigation */}
                      <div className="w-64 bg-muted/50 border-r border-border p-4 overflow-y-auto">
            <div className="mb-6">
              <h2 className="text-xl font-semibold">Settings</h2>
            </div>
            
            <nav className="space-y-2">
              {tabs.map((tab) => {
                const Icon = tab.icon;
                return (
                  <Button
                    key={tab.id}
                    variant={activeTab === tab.id ? "secondary" : "ghost"}
                    className={cn(
                      "w-full justify-start h-auto p-3 whitespace-normal",
                      activeTab === tab.id && "bg-secondary"
                    )}
                    onClick={() => setActiveTab(tab.id)}
                  >
                    <Icon className="h-5 w-5 mr-3 flex-shrink-0" />
                    <div className="text-left flex-1 min-w-0">
                      <div className="font-medium">{tab.label}</div>
                      <div className="text-xs text-muted-foreground break-words">
                        {tab.description}
                      </div>
                    </div>
                  </Button>
                );
              })}
            </nav>
          </div>
          
                                {/* Right Content Area */}
                      <div className="flex-1 w-0 overflow-y-auto h-full max-h-full min-h-0">
                        <div className="h-full min-h-0">
                          {renderContent()}
                        </div>
                      </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

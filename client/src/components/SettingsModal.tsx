"use client";

import React, { useState } from 'react';
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Settings, User, X, BookOpen, FileText } from "lucide-react";
import { cn } from "@/lib/utils";
import { GeneralSettings } from "@/components/GeneralSettings";
import { ProfileSettings } from "@/components/ProfileSettings";
import { KnowledgeBaseSettings } from "@/components/KnowledgeBaseSettings";
import { PostmortemsSettings } from "@/components/PostmortemsSettings";


interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type SettingsTab = 'general' | 'profile' | 'knowledge-base' | 'postmortems';

export function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>('general');

  const tabs = [
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
    }
  ];

  const renderContent = () => {
    switch (activeTab) {
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
                      <div className="flex-1 overflow-y-auto h-full max-h-full min-h-0 pr-8">
                        <div className="h-full min-h-0">
                          {renderContent()}
                        </div>
                      </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
